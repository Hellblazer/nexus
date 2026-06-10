// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.vectors.ChromaQuotaValidator;
import dev.nexus.service.vectors.ChromaRestClient;
import dev.nexus.service.vectors.LocalChromaServer;
import dev.nexus.service.vectors.OnnxEmbedder;
import dev.nexus.service.vectors.VectorRepository;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Arrays;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * Surviving Chroma machinery — repo-direct integration coverage
 * (RDR-155 P4a.2, bead nexus-1k8s1).
 *
 * <p>HISTORY: this class originally exercised the {@code /v1/vectors/*} HTTP serving
 * surface against a live local Chroma (RDR-152 bead nexus-gmiaf.20). The RDR-155
 * Phase 4a serving cutover routes that surface exclusively to pgvector — the HTTP
 * serving contract now lives in {@code PgVectorServingContractTest} (hermetic,
 * Testcontainers). What remains HERE is the coverage the P4a survival pins promise:
 * the four surviving Chroma classes ({@link VectorRepository},
 * {@link ChromaRestClient}, {@link LocalChromaServer}, {@link ChromaQuotaValidator})
 * stay RUNNABLE as the Phase-5 ETL read machinery and the parity/dual-run comparand
 * fixtures, until Phase 4b (gated on P5.G) deletes them.
 *
 * <p>All access is repo-direct — no {@code NexusService}, no Postgres.
 *
 * <p>Requires: {@code chroma} CLI on PATH (or NX_CHROMA_BINARY set), ONNX model
 * files in the chromadb cache. Marked {@code @Tag("integration")} — run via
 * {@code mvn test -Dtest=VectorIntegrationTest -Dtest.excluded.groups="" -Dgroups=integration}.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class VectorIntegrationTest {

    private static final String COLLECTION = "knowledge__nexus-test__all-minilm-l6-v2__v1";

    LocalChromaServer localChroma;
    OnnxEmbedder      onnxEmbedder;
    VectorRepository  vectorRepo;
    Path              chromaData;

    @BeforeAll
    void startAll() throws Exception {
        String chromaBinary = LocalChromaServer.findChromaBinary();
        chromaData = Files.createTempDirectory("vector-it-chroma-");
        int chromaPort = LocalChromaServer.findFreePort();

        localChroma = new LocalChromaServer(chromaBinary, chromaData.toString(), chromaPort);
        localChroma.start();

        onnxEmbedder = new OnnxEmbedder();
        ChromaRestClient chromaClient = ChromaRestClient.local("127.0.0.1", chromaPort);
        vectorRepo = new VectorRepository(onnxEmbedder, onnxEmbedder, chromaClient);
    }

    @AfterAll
    void stopAll() throws Exception {
        if (onnxEmbedder != null) onnxEmbedder.close();
        if (localChroma  != null) localChroma.stop();
        if (chromaData   != null) {
            try (var walk = Files.walk(chromaData)) {
                walk.sorted(java.util.Comparator.reverseOrder())
                    .forEach(p -> p.toFile().delete());
            }
        }
    }

    // ── Read-leg round trip: the machinery the Phase-5 ETL reads through ──────

    @Test
    void upsertChunks_thenSearch_returnsResults() {
        vectorRepo.upsertChunks(COLLECTION,
                List.of("chunk-aa", "chunk-bb", "chunk-cc"),
                List.of(
                        "The sky is blue and birds fly through it.",
                        "Deep neural networks learn representations from data.",
                        "Postgres is a relational database with MVCC."),
                List.of(
                        Map.of("topic", "nature"),
                        Map.of("topic", "ml"),
                        Map.of("topic", "database")));

        var results = vectorRepo.search(
                "neural network machine learning", List.of(COLLECTION), 3, null);
        assertThat(results).isNotEmpty();
        assertThat(results.get(0).get("id"))
                .as("the ML chunk is the closest result")
                .isEqualTo("chunk-bb");

        // Paged read APIs the ETL leans on: get-by-ids and list.
        Map<String, Object> got = vectorRepo.get(COLLECTION, List.of("chunk-bb"), 10, 0);
        assertThat((List<Object>) got.get("ids")).containsExactly("chunk-bb");
        Map<String, Object> listed = vectorRepo.list(COLLECTION, 10, 0);
        assertThat((List<?>) listed.get("ids")).hasSize(3);
        assertThat(vectorRepo.count(COLLECTION)).isEqualTo(3);
    }

    // ── ONNX embed determinism (bit-exact) + round-trip distance ─────────────

    @Test
    void onnxEmbedParity_bitExactDeterminism_andChromaRoundTrip() {
        String text = "Semantic search connects questions to answers through meaning.";
        String chunkId = "parity-chunk-01";

        // Part A: bit-exact vector determinism (S0.2 self-consistency check).
        float[] v1 = onnxEmbedder.embedOne(text);
        float[] v2 = onnxEmbedder.embedOne(text);
        assertThat(Arrays.equals(v1, v2))
                .as("OnnxEmbedder must be bit-exactly deterministic for the same input text")
                .isTrue();

        // Part B: Chroma round-trip distance < 1e-6. Chroma computes cosine distance
        // in float32 internally; an identical stored vector yields ~1e-7 (NOT
        // bit-exact 0.0). Real embedding drift (e.g. the Voyage truncation=false bug,
        // dist ≈ 5e-5) is still caught: 5e-5 >> 1e-6.
        String parityCol = COLLECTION + "-parity";
        vectorRepo.upsertChunks(parityCol,
                List.of(chunkId), List.of(text), List.of(Map.of("test", "parity")));
        var results = vectorRepo.search(text, List.of(parityCol), 1, null);
        assertThat(results).hasSize(1);
        assertThat(results.get(0).get("id")).isEqualTo(chunkId);
        double dist = ((Number) results.get(0).get("distance")).doubleValue();
        assertThat(Math.abs(dist))
                .as("Chroma cosine distance for identical text must be < 1e-6 "
                    + "(float32 epsilon is ~1e-7; real drift is ≥ 5e-5)")
                .isLessThan(1e-6);
    }

    // ── Quota enforcement on the surviving read-client machinery ─────────────

    @Test
    void quotaViolation_oversizedDocument_failsLoud() {
        // chroma_quotas survives Phase 4a — it still governs the surviving Chroma
        // leg (the pgvector serving path has no such quota by design, RDR-155 §Retire).
        String oversized = "x".repeat(ChromaQuotaValidator.MAX_DOCUMENT_BYTES + 1);

        assertThatThrownBy(() -> vectorRepo.upsertChunks(COLLECTION,
                List.of("oversized-id"), List.of(oversized), List.of(Map.of())))
                .isInstanceOf(ChromaQuotaValidator.QuotaViolation.class);
    }

    // ── Clean shutdown kills the Chroma child (no orphan) ─────────────────────

    @Test
    void localChromaShutdown_noOrphan() throws Exception {
        // Create a second LocalChromaServer on a separate port and data dir
        String chromaBinary = LocalChromaServer.findChromaBinary();
        Path tempData = Files.createTempDirectory("chroma-orphan-test-");
        int tempPort = LocalChromaServer.findFreePort();

        LocalChromaServer temp = new LocalChromaServer(chromaBinary, tempData.toString(), tempPort);
        temp.start();
        assertThat(temp.isAlive()).as("chroma process alive after start").isTrue();

        temp.stop();
        assertThat(temp.isAlive()).as("chroma process dead after stop").isFalse();

        // Cleanup temp dir
        try (var walk = Files.walk(tempData)) {
            walk.sorted(java.util.Comparator.reverseOrder())
                .map(Path::toFile)
                .forEach(java.io.File::delete);
        } catch (IOException ignored) {}
    }
}
