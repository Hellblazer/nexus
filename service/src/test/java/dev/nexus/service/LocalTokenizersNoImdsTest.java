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
import java.util.concurrent.TimeUnit;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

/**
 * nexus-17jvq -- the local engine never dials the AWS instance-metadata address.
 *
 * <p>The probe is DJL's, not an AWS SDK's (the engine has none): {@code HuggingFaceTokenizer} calls
 * {@code ai.djl.util.Ec2Utils.callHome} on every tokenizer build, which asks
 * {@code http://169.254.169.254/latest/api/token} for an IMDSv2 token and then reads the instance id and region,
 * unless {@code OPT_OUT_TRACKING} is set. On a real EC2 host that reads the host's instance identity and posts it
 * to a DJL telemetry bucket.
 *
 * <p>The witness is a child JVM that installs a {@link URLStreamHandlerFactory} for http and https before it builds
 * a tokenizer: it records each URL the library opens and refuses it, so nothing leaves the machine. A child, not
 * this JVM, because the factory can be set once per JVM and {@code Ec2Utils} remembers its last check-in in a static.
 * (A {@code ProxySelector} would see nothing: {@code Ec2Utils} opens its connection with {@code Proxy.NO_PROXY}.)
 * The control child builds the tokenizer with DJL's own builder and must be seen to probe, or the clean engine child
 * proves nothing.
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
        assumeTrue(System.getenv(LocalTokenizers.OPT_OUT_PROPERTY) == null, "OPT_OUT_TRACKING is set in the environment");
        Path tok = dir.resolve("tokenizer-" + mode + ".json");
        Files.writeString(tok, TINY_TOKENIZER);
        Process p = new ProcessBuilder(
                Path.of(System.getProperty("java.home"), "bin", "java").toString(),
                "-cp", System.getProperty("java.class.path"),
                Child.class.getName(), mode, tok.toString())
            .redirectErrorStream(true).start();
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
    void control_anUnguardedDjlBuildAsksTheMetadataService() throws Exception {
        assertThat(requestsFrom("control"))
            .as("without the opt-out DJL's own build asks %s; if this is empty the witness cannot see the probe", IMDS_HOST)
            .anyMatch(u -> u.contains(IMDS_HOST));
    }

    @Test
    void optOutIsIdempotent_andLeavesAnOperatorValueAlone() {
        String before = System.getProperty(LocalTokenizers.OPT_OUT_PROPERTY);
        try {
            System.setProperty(LocalTokenizers.OPT_OUT_PROPERTY, "false");
            LocalTokenizers.optOutOfTelemetry();
            assertThat(System.getProperty(LocalTokenizers.OPT_OUT_PROPERTY)).isEqualTo("false");

            System.clearProperty(LocalTokenizers.OPT_OUT_PROPERTY);
            LocalTokenizers.optOutOfTelemetry();
            LocalTokenizers.optOutOfTelemetry();
            assertThat(System.getProperty(LocalTokenizers.OPT_OUT_PROPERTY)).isEqualTo("true");
        } finally {
            if (before == null) {
                System.clearProperty(LocalTokenizers.OPT_OUT_PROPERTY);
            } else {
                System.setProperty(LocalTokenizers.OPT_OUT_PROPERTY, before);
            }
        }
    }

    /**
     * Census: the production classes that build a tokenizer do it through {@link LocalTokenizers}, so a fourth
     * direct {@code HuggingFaceTokenizer.builder()} cannot reopen the probe unseen. Non-vacuous: it must read the
     * engine sources, find the three known callers and exactly the helper's own builder call.
     */
    @Test
    void everyProductionTokenizerBuildGoesThroughTheHelper() throws Exception {
        Path root = Path.of("src/main/java");
        int examined = 0;
        int directBuilders = 0;
        int viaHelper = 0;
        try (Stream<Path> files = Files.walk(root)) {
            for (Path f : files.filter(p -> p.toString().endsWith(".java")).toList()) {
                String src = Files.readString(f);
                examined++;
                if (src.contains("HuggingFaceTokenizer.builder()")) {
                    directBuilders++;
                    assertThat(f.getFileName().toString()).as("only the helper may call the DJL builder directly")
                        .isEqualTo("LocalTokenizers.java");
                }
                if (src.contains("LocalTokenizers.build(")) {
                    viaHelper++;
                }
            }
        }
        assertThat(examined).as("the census read the engine sources").isGreaterThan(50);
        assertThat(directBuilders).as("exactly one direct DJL builder call: the helper's").isEqualTo(1);
        assertThat(viaHelper).as("Bge768Embedder, CrossEncoderReranker, OnnxEmbedder").isEqualTo(3);
    }
}
