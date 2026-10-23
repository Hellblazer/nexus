// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.vectors.BootPlan;
import dev.nexus.service.vectors.CceEmbedder;
import dev.nexus.service.vectors.EmbedderRouter;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.EmbeddingProfile;
import dev.nexus.service.vectors.LocalModelProbe;
import dev.nexus.service.vectors.LocalOnnxAdmission;
import dev.nexus.service.vectors.Reranker;
import dev.nexus.service.vectors.VoyageEmbedder;
import dev.nexus.service.vectors.VoyageReranker;
import org.junit.jupiter.api.Test;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-210 Technical Design 7 (nexus-3va7p): the runtime half of the Voyage egress pin, with no network.
 *
 * <p>{@code EgressGuardCensus} pins the host by reading each Voyage class's {@code static final String *URL*}
 * constant. The clients also take a {@code url} constructor argument, so the constant is only the default and
 * the text scan cannot tell whether the default constructor reaches it. This test builds each client with its
 * public default constructor in a child JVM whose {@code NX_HTTPS_PROXY} names a recording proxy on port 0,
 * makes one request, and reads the {@code CONNECT host:port} line the JDK client sends to the proxy: that line
 * is the host the shipped constructor actually dials, and the recording proxy refuses it, so nothing leaves the
 * machine. The same child JVM is the "keyless boot" check: with no key, the router and the reranker plan build
 * no Voyage client, which shows as no {@code HttpClient-*-SelectorManager} thread (the JDK starts one the
 * moment an {@code HttpClient} is built), with a keyed positive control proving the detector sees one.
 *
 * <p>It does not cover Main itself (the wiring order is pinned by {@code MainEmbedProfileWiringTest}), a
 * dependency's own network use, or the GraalVM native image.
 */
class EgressVoyageClientWitnessTest {

    static final String VOYAGE_CONNECT = "api.voyageai.com:443";

    /** The child: arg 0 is the mode; prints evidence lines then DONE. */
    public static final class Child {
        public static void main(String[] args) throws Exception {
            switch (args[0]) {
                case "embedder" -> attempt(() -> new VoyageEmbedder("witness-key", "voyage-code-3", "document")
                    .embed(List.of("x")));
                case "cce" -> attempt(() -> new CceEmbedder("witness-key", "document").embed(List.of("x")));
                case "reranker" -> attempt(() -> new VoyageReranker("witness-key", VoyageReranker.DEFAULT_MODEL)
                    .rerank("q", List.of("a", "b"), null));
                case "keyless" -> keyless();
                case "keyed" -> keyed();
                default -> throw new IllegalArgumentException(args[0]);
            }
            System.out.println("DONE");
        }

        private interface Call {
            void run() throws Exception;
        }

        /** The request is expected to fail (the proxy refuses the tunnel); the CONNECT line is the evidence. */
        private static void attempt(Call c) {
            try {
                c.run();
                System.out.println("UNEXPECTED_SUCCESS");
            } catch (Throwable expected) {
                System.out.println("REFUSED " + expected.getClass().getSimpleName());
            }
        }

        private static Embedder localFake() {
            return texts -> List.of(new float[768]);
        }

        private static Reranker fakeReranker() {
            return new Reranker() {
                @Override public String modelToken() { return "fake-local"; }
                @Override public List<Scored> rerank(String q, List<String> d, Integer k) { return List.of(); }
            };
        }

        private static LocalModelProbe.Presence presence(boolean key) {
            return new LocalModelProbe.Presence(true, false, key, "bge", List.of(), true, "ce", List.of());
        }

        private static long httpClientThreads() {
            return Thread.getAllStackTraces().keySet().stream()
                .filter(t -> t.getName().matches("HttpClient-\\d+-SelectorManager")).count();
        }

        /** Every keyless shape Main can reach builds no Voyage client. */
        private static void keyless() {
            System.out.println("BASELINE " + httpClientThreads());
            for (String key : new String[] {null, "", "   "}) {
                for (EmbeddingProfile profile : new EmbeddingProfile[] {null, EmbeddingProfile.BGE, EmbeddingProfile.VOYAGE}) {
                    EmbedderRouter r = new EmbedderRouter(localFake(), key, "document", profile);
                    System.out.println("ROUTER key=" + (key == null ? "null" : "'" + key + "'") + " profile=" + profile
                        + " mode=" + r.modeName() + " models=" + r.availableModels());
                }
            }
            for (EmbeddingProfile profile : EmbeddingProfile.values()) {
                BootPlan plan = BootPlan.decide(presence(false), profile);
                // Main's supplier, verbatim: it must never run without a key.
                Reranker built = plan.buildReranker(
                    () -> new VoyageReranker(null, VoyageReranker.DEFAULT_MODEL), Child::fakeReranker,
                    new LocalOnnxAdmission(1, 1000));
                System.out.println("RERANKER profile=" + profile + " token=" + built.modelToken());
            }
            System.out.println("HTTP_CLIENT_THREADS " + httpClientThreads());
        }

        /** The positive control: a keyed router does build Voyage clients, and the detector sees them. */
        private static void keyed() {
            System.out.println("BASELINE " + httpClientThreads());
            EmbedderRouter r = new EmbedderRouter(localFake(), "witness-key", "document", EmbeddingProfile.VOYAGE);
            System.out.println("ROUTER mode=" + r.modeName() + " models=" + r.availableModels());
            System.out.println("HTTP_CLIENT_THREADS " + httpClientThreads());
        }
    }

    /** A proxy that records the first line of each connection and refuses the tunnel. */
    private static final class RecordingProxy implements AutoCloseable {
        final ServerSocket server;
        final List<String> requestLines = new CopyOnWriteArrayList<>();
        private final Thread acceptor;

        RecordingProxy() throws IOException {
            server = new ServerSocket(0, 50, InetAddress.getLoopbackAddress());
            acceptor = new Thread(() -> {
                while (!server.isClosed()) {
                    try (Socket s = server.accept()) {
                        BufferedReader in = new BufferedReader(
                            new InputStreamReader(s.getInputStream(), StandardCharsets.US_ASCII));
                        String line = in.readLine();
                        if (line != null) {
                            requestLines.add(line);
                        }
                        s.getOutputStream().write(
                            "HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                                .getBytes(StandardCharsets.US_ASCII));
                    } catch (IOException e) {
                        // closed, or a client that went away
                    }
                }
            }, "egress-recording-proxy");
            acceptor.setDaemon(true);
            acceptor.start();
        }

        int port() {
            return server.getLocalPort();
        }

        @Override
        public void close() throws IOException {
            server.close();
        }
    }

    /** Runs the child; returns its output lines. Kills it once {@code stopWhen} holds (the retry loop is slow). */
    private static List<String> runChild(String mode, RecordingProxy proxy, boolean killOnFirstRequest)
        throws Exception {
        List<String> cmd = new ArrayList<>(List.of(
            Path.of(System.getProperty("java.home"), "bin", "java").toString(),
            "-cp", System.getProperty("java.class.path"), Child.class.getName(), mode));
        ProcessBuilder pb = new ProcessBuilder(cmd).redirectErrorStream(true);
        pb.environment().remove("HTTPS_PROXY");
        pb.environment().remove("NX_VOYAGE_API_KEY");
        pb.environment().put("NX_HTTPS_PROXY", "127.0.0.1:" + proxy.port());
        Process p = pb.start();
        List<String> lines = new CopyOnWriteArrayList<>();
        Thread reader = new Thread(() -> {
            try (BufferedReader r = new BufferedReader(new InputStreamReader(p.getInputStream(), StandardCharsets.UTF_8))) {
                for (String line; (line = r.readLine()) != null; ) {
                    lines.add(line);
                }
            } catch (IOException ignored) {
                // child killed
            }
        });
        reader.setDaemon(true);
        reader.start();
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(120);
        while (p.isAlive() && System.nanoTime() < deadline) {
            if (killOnFirstRequest && !proxy.requestLines.isEmpty()) {
                break;
            }
            p.waitFor(100, TimeUnit.MILLISECONDS);
        }
        boolean finished = !p.isAlive();
        p.destroyForcibly();
        p.waitFor(30, TimeUnit.SECONDS);
        reader.join(5_000);
        if (!killOnFirstRequest) {
            assertThat(finished).as("the %s child finished: %s", mode, lines).isTrue();
            assertThat(p.exitValue()).as("the %s child exit: %s", mode, lines).isZero();
            assertThat(lines).as("the %s child ran to the end", mode).contains("DONE");
        }
        return lines;
    }

    /** The hosts the recording proxy was asked to tunnel to. */
    static List<String> connectTargets(List<String> requestLines) {
        return requestLines.stream().filter(l -> l.startsWith("CONNECT ")).map(l -> l.split(" ")[1]).toList();
    }

    // ---- (3) the default constructors dial api.voyageai.com ---------------------------------------

    @Test
    void theDefaultConstructorsOfTheThreeVoyageClientsDialOnlyApiVoyageaiCom() throws Exception {
        for (String mode : new String[] {"embedder", "cce", "reranker"}) {
            try (RecordingProxy proxy = new RecordingProxy()) {
                runChild(mode, proxy, true);
                assertThat(connectTargets(proxy.requestLines))
                    .as("non-vacuity: the %s client's default constructor made a request through the proxy "
                        + "(saw %s)", mode, proxy.requestLines)
                    .isNotEmpty();
                assertThat(proxy.requestLines).as("the %s client sent nothing but CONNECT lines", mode)
                    .allMatch(l -> l.startsWith("CONNECT "));
                assertThat(connectTargets(proxy.requestLines)).as("%s client dials only the Voyage host", mode)
                    .allMatch(VOYAGE_CONNECT::equals);
            }
        }
    }

    @Test
    void fixture_theDetectorReportsAHostOtherThanVoyage() {
        List<String> lines = List.of("CONNECT api.voyageai.com:443 HTTP/1.1", "CONNECT telemetry.example:443 HTTP/1.1",
            "GET http://x/ HTTP/1.1");
        assertThat(connectTargets(lines)).containsExactly("api.voyageai.com:443", "telemetry.example:443");
        assertThat(connectTargets(lines)).as("a second host would fail the pin").anyMatch(h -> !VOYAGE_CONNECT.equals(h));
    }

    // ---- (4) a keyless boot builds no Voyage client ------------------------------------------------

    @Test
    void aKeylessBootShapeConstructsNoVoyageClient_andAKeyedOneDoes() throws Exception {
        try (RecordingProxy proxy = new RecordingProxy()) {
            List<String> keyed = runChild("keyed", proxy, false);
            long keyedThreads = threadsLine(keyed);
            long keyedBase = baseline(keyed);
            assertThat(keyedThreads).as("positive control: a keyed router builds Voyage HttpClients, and the "
                + "detector sees their selector threads (%s)", keyed).isGreaterThan(keyedBase);

            List<String> keyless = runChild("keyless", proxy, false);
            assertThat(keyless).as("non-vacuity: all nine router shapes and both reranker plans ran")
                .filteredOn(l -> l.startsWith("ROUTER ")).hasSize(9)
                .allMatch(l -> l.contains("mode=onnx-local") && !l.contains("voyage-"));
            assertThat(keyless).filteredOn(l -> l.startsWith("RERANKER ")).hasSize(2)
                .allMatch(l -> l.contains("token=fake-local"));
            assertThat(threadsLine(keyless)).as("a keyless boot started no HttpClient (%s)", keyless)
                .isEqualTo(baseline(keyless));
            assertThat(proxy.requestLines).as("and dialed nothing").isEmpty();
        }
    }

    private static long threadsLine(List<String> lines) {
        return lines.stream().filter(l -> l.startsWith("HTTP_CLIENT_THREADS ")).mapToLong(l -> Long.parseLong(l.split(" ")[1]))
            .findFirst().orElseThrow(() -> new AssertionError("no HTTP_CLIENT_THREADS line in " + lines));
    }

    private static long baseline(List<String> lines) {
        return lines.stream().filter(l -> l.startsWith("BASELINE ")).mapToLong(l -> Long.parseLong(l.split(" ")[1]))
            .findFirst().orElseThrow(() -> new AssertionError("no BASELINE line in " + lines));
    }
}
