// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import ai.djl.huggingface.tokenizers.HuggingFaceTokenizer;
import dev.nexus.service.vectors.Bge768Embedder;
import dev.nexus.service.vectors.CrossEncoderReranker;
import dev.nexus.service.vectors.LocalTokenizers;
import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestMethodOrder;
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

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.assertj.core.api.Assertions.fail;

/**
 * RDR-210 Technical Design 7 (nexus-qfy01): a runtime check beside the source census. DJL's tokenizer build
 * was the one place the engine's own libraries opened an outbound stream through {@code java.net.URL}
 * (nexus-17jvq). This witness is a child JVM that installs a {@code URLStreamHandlerFactory} refusing every
 * stream that is not {@code file} or {@code jar}, records each attempt, then builds the engine's local
 * models the way the engine does and runs one embed and one rerank.
 *
 * <p>Two legs. The tokenizer leg always runs: it builds a tiny tokenizer through {@link LocalTokenizers}, the
 * helper that Bge768Embedder, CrossEncoderReranker and OnnxEmbedder share, so the witness has a target on a
 * host with no model. The model leg builds the real bge embedder and cross-encoder and needs the provisioned
 * model files; it skips when nothing is provisioned (CI), fails when {@value #REQUIRE_ENV}=1 names them
 * required, and fails when the model directory exists but is incomplete, so a half provision is not a silent
 * pass. The tokenizer leg and the negative fixtures below are what keep a skipped model leg from leaving the
 * witness empty.
 *
 * <p>What it does not cover, and no test in this repository does: {@code java.net.http} use inside a
 * dependency (it bypasses {@code URL}), raw sockets, DNS or JNI inside a dependency (DJL's Rust tokenizers
 * library does network I/O that a URL factory cannot see), and the GraalVM native image the engine ships as
 * (this runs on a JVM). {@code EgressGuardCensus} scans only the engine's own sources and does not close
 * these gaps. A child, because the factory can be set once per JVM.
 */
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class EgressRuntimeWitnessTest {

    static final String REQUIRE_ENV = "NX_REQUIRE_ORT_MODEL";

    /** Mirrors the bge provisioner's floor and Bge768Embedder.MIN_MODEL_BYTES. */
    private static final long MIN_BGE_BYTES = 200_000_000L;

    /** A WordLevel tokenizer the DJL native library can load; no model download, no network. */
    private static final String TINY_TOKENIZER =
        "{\"version\":\"1.0\",\"truncation\":null,\"padding\":null,\"added_tokens\":[],"
        + "\"normalizer\":null,\"pre_tokenizer\":{\"type\":\"Whitespace\"},\"post_processor\":null,"
        + "\"decoder\":null,\"model\":{\"type\":\"WordLevel\",\"vocab\":{\"[UNK]\":0,\"hello\":1},"
        + "\"unk_token\":\"[UNK]\"}}";

    @TempDir Path dir;

    /**
     * The child JVM. Args: mode, tokenizer path, then (model leg only) bge model, bge tokenizer, cross-encoder
     * model, cross-encoder tokenizer. Prints {@code REQUEST <url>} for every refused open, then {@code DONE}.
     */
    public static final class Child {
        public static void main(String[] args) throws Exception {
            URL.setURLStreamHandlerFactory(protocol ->
                ("file".equals(protocol) || "jar".equals(protocol)) ? null : new URLStreamHandler() {
                    @Override protected URLConnection openConnection(URL u) throws IOException {
                        System.out.println("REQUEST " + u);
                        throw new IOException("witness refuses " + u);
                    }
                    @Override protected URLConnection openConnection(URL u, Proxy p) throws IOException {
                        return openConnection(u);
                    }
                });
            String mode = args[0];
            Path tok = Path.of(args[1]);
            switch (mode) {
                case "tokenizer" -> {
                    try (HuggingFaceTokenizer t = LocalTokenizers.build(tok.toString(), 16)) {
                        System.out.println("TOKENS " + t.encode("hello").getTokens().length);
                    }
                }
                case "models" -> {
                    Bge768Embedder bge = new Bge768Embedder(args[2], args[3]);
                    CrossEncoderReranker ce = new CrossEncoderReranker(args[4], args[5]);
                    try {
                        System.out.println("EMBED " + bge.embed(List.of("hello world")).get(0).length);
                        System.out.println("RERANK " + ce.rerank("hello", List.of("hello world", "goodbye"), null).size());
                    } finally {
                        ce.close();
                        bge.close();
                    }
                }
                // Negative controls: the witness must see an outbound open when one happens.
                case "control-djl" -> {
                    try (HuggingFaceTokenizer t = HuggingFaceTokenizer.builder().optTokenizerPath(tok).build()) {
                        System.out.println("TOKENS " + t.encode("hello").getTokens().length);
                    }
                }
                case "control-http" -> {
                    try (var in = new URL("http://egress-witness.invalid/probe").openStream()) {
                        in.read();
                    } catch (IOException expected) {
                        // refused by the factory; the REQUEST line is the evidence
                    }
                }
                default -> throw new IllegalArgumentException(mode);
            }
            System.out.println("DONE");
        }
    }

    /** The child's output lines. Runs it to the end; fails unless it printed DONE. */
    private List<String> run(String mode, String... modelPaths) throws Exception {
        Path tok = dir.resolve("tokenizer-" + mode + ".json");
        Files.writeString(tok, TINY_TOKENIZER);
        List<String> cmd = new ArrayList<>(List.of(
            Path.of(System.getProperty("java.home"), "bin", "java").toString(),
            "-cp", System.getProperty("java.class.path"),
            Child.class.getName(), mode, tok.toString()));
        cmd.addAll(List.of(modelPaths));
        ProcessBuilder pb = new ProcessBuilder(cmd).redirectErrorStream(true);
        // The result must not depend on the developer's shell: DJL reads these.
        pb.environment().remove(LocalTokenizers.OPT_OUT_PROPERTY);
        pb.environment().remove(LocalTokenizers.OFFLINE_ENV);
        Process p = pb.start();
        List<String> lines = new ArrayList<>();
        try (BufferedReader r = new BufferedReader(new InputStreamReader(p.getInputStream(), StandardCharsets.UTF_8))) {
            for (String line; (line = r.readLine()) != null; ) {
                lines.add(line);
            }
        }
        assertThat(p.waitFor(300, TimeUnit.SECONDS)).as("the %s child exits", mode).isTrue();
        assertThat(p.exitValue()).as("the %s child ran to the end: %s", mode, lines).isZero();
        assertThat(lines).as("the %s child finished", mode).contains("DONE");
        return lines;
    }

    /** The witness verdict: no refused open appears among the child's output. */
    static void assertNoOutboundOpen(List<String> childLines) {
        assertThat(childLines.stream().filter(l -> l.startsWith("REQUEST ")).toList())
            .as("outbound stream opens the engine's library code attempted")
            .isEmpty();
    }

    // ---- the witness -------------------------------------------------------------------------------

    @Test
    void theTokenizerBuildTheEnginesShareOpensNothingOutbound() throws Exception {
        List<String> lines = run("tokenizer");
        assertThat(lines).as("non-vacuity: the tokenizer was built and used").contains("TOKENS 1");
        assertNoOutboundOpen(lines);
    }

    /** Set by the model leg when it skips; read by the max-skip guard below, which runs last. */
    private static volatile boolean modelLegSkipped;

    @Test
    @Order(1)
    void theBgeEmbedderAndTheCrossEncoderOpenNothingOutboundThroughAnEmbedAndARerank() throws Exception {
        String[] paths = requireModelsOrSkip();
        List<String> lines = run("models", paths);
        assertThat(lines).as("non-vacuity: one embed and one rerank ran").contains("EMBED 768", "RERANK 2");
        assertNoOutboundOpen(lines);
    }

    // ---- negative fixtures: the witness is not vacuous ---------------------------------------------

    @Test
    void fixture_aTokenizerBuildThatOpensAnHttpStreamFailsTheWitness() throws Exception {
        List<String> lines = run("control-djl");
        assertThat(lines).as("DJL's own unguarded build must be seen to reach for the network")
            .anyMatch(l -> l.startsWith("REQUEST http://"));
        assertThatThrownBy(() -> assertNoOutboundOpen(lines)).isInstanceOf(AssertionError.class);
    }

    @Test
    void fixture_aDirectHttpUrlStreamFailsTheWitness() throws Exception {
        List<String> lines = run("control-http");
        assertThat(lines).contains("REQUEST http://egress-witness.invalid/probe");
        assertThatThrownBy(() -> assertNoOutboundOpen(lines)).isInstanceOf(AssertionError.class);
    }

    // ---- the model files ---------------------------------------------------------------------------

    private static String[] defaultModelPaths() {
        return new String[] {Bge768Embedder.DEFAULT_MODEL_PATH, Bge768Embedder.DEFAULT_TOKENIZER_PATH,
                             CrossEncoderReranker.DEFAULT_MODEL_PATH, CrossEncoderReranker.DEFAULT_TOKENIZER_PATH};
    }

    /** What the model leg does for a given set of model files. */
    enum LegVerdict { RUN, SKIP, FAIL_REQUIRED, FAIL_PARTIAL }

    /**
     * The model leg's decision. {@code RUN} whenever the files are all present, whatever else is set; a skip
     * is possible only when nothing is provisioned and the leg is not required.
     */
    static LegVerdict legVerdict(String[] paths, long minBgeBytes, boolean required) {
        boolean complete = size(paths[0]) >= minBgeBytes && size(paths[1]) > 0
            && size(paths[2]) > 0 && size(paths[3]) > 0;
        if (complete) {
            return LegVerdict.RUN;
        }
        if (required) {
            return LegVerdict.FAIL_REQUIRED;
        }
        boolean partial = Files.isDirectory(Path.of(paths[0]).getParent())
            || Files.isDirectory(Path.of(paths[2]).getParent());
        return partial ? LegVerdict.FAIL_PARTIAL : LegVerdict.SKIP;
    }

    /**
     * The four model paths, or a skip. A skip means nothing is provisioned; a partial provision fails, and so
     * does {@value #REQUIRE_ENV}=1 with the files absent.
     */
    private static String[] requireModelsOrSkip() {
        String[] paths = defaultModelPaths();
        LegVerdict verdict = legVerdict(paths, MIN_BGE_BYTES, "1".equals(System.getenv(REQUIRE_ENV)));
        String why = "bge or cross-encoder model files not complete under " + Path.of(paths[0]).getParent().getParent();
        switch (verdict) {
            case RUN -> {
                return paths;
            }
            case FAIL_REQUIRED -> fail("%s=1 but %s; provision them with `nx init --service`", REQUIRE_ENV, why);
            case FAIL_PARTIAL -> fail("a model directory exists but is incomplete (%s): "
                + "a half provision must not skip this witness", why);
            case SKIP -> {
                modelLegSkipped = true;
                Assumptions.assumeTrue(false, "SKIPPED (not passed): " + why
                    + ". The tokenizer leg and the fixtures still ran.");
            }
        }
        return paths;
    }

    // ---- max-skip: a skipped model leg is never a silent pass on a host that has the files ---------

    @Test
    @Order(Integer.MAX_VALUE)
    void maxSkip_theModelLegDidNotSkipOnAHostWhereTheFilesArePresent() {
        LegVerdict verdict = legVerdict(defaultModelPaths(), MIN_BGE_BYTES, false);
        System.out.println("EGRESS_WITNESS modelLegVerdict=" + verdict + " skipped=" + modelLegSkipped);
        if (verdict == LegVerdict.RUN) {
            assertThat(modelLegSkipped).as("the model files are present, so the model leg must have run").isFalse();
        }
    }

    @Test
    void fixture_theLegVerdictRunsWhenFilesArePresent_andSkipsOnlyWhenNothingIsProvisioned() throws IOException {
        Path root = dir.resolve("models");
        String[] paths = {root.resolve("bge/model.onnx").toString(), root.resolve("bge/tokenizer.json").toString(),
                          root.resolve("ce/model.onnx").toString(), root.resolve("ce/tokenizer.json").toString()};
        assertThat(legVerdict(paths, 10, false)).as("nothing provisioned").isEqualTo(LegVerdict.SKIP);
        assertThat(legVerdict(paths, 10, true)).as("nothing provisioned, required").isEqualTo(LegVerdict.FAIL_REQUIRED);

        Files.createDirectories(root.resolve("bge"));
        Files.writeString(root.resolve("bge/model.onnx"), "x".repeat(10));
        assertThat(legVerdict(paths, 10, false)).as("half provisioned").isEqualTo(LegVerdict.FAIL_PARTIAL);

        Files.writeString(root.resolve("bge/tokenizer.json"), "{}");
        Files.createDirectories(root.resolve("ce"));
        Files.writeString(root.resolve("ce/model.onnx"), "x");
        Files.writeString(root.resolve("ce/tokenizer.json"), "{}");
        assertThat(legVerdict(paths, 10, false)).as("all present").isEqualTo(LegVerdict.RUN);
        assertThat(legVerdict(paths, 10, true)).as("all present, required").isEqualTo(LegVerdict.RUN);
        assertThat(legVerdict(paths, 11, false)).as("bge model under the size floor")
            .isEqualTo(LegVerdict.FAIL_PARTIAL);
    }

    private static long size(String path) {
        try {
            return Files.isRegularFile(Path.of(path)) ? Files.size(Path.of(path)) : -1;
        } catch (IOException e) {
            return -1;
        }
    }
}
