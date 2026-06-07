package dev.nexus.spike.s04;

import ai.djl.huggingface.tokenizers.Encoding;
import ai.djl.huggingface.tokenizers.HuggingFaceTokenizer;
import ai.onnxruntime.OnnxTensor;
import ai.onnxruntime.OrtEnvironment;
import ai.onnxruntime.OrtSession;
import org.jooq.DSLContext;
import org.jooq.Record;
import org.jooq.Result;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;

import java.nio.LongBuffer;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.DriverManager;
import java.util.List;
import java.util.Map;

/**
 * RDR-152 S0.4 — combined end-to-end proof.
 * <p>
 * Args: <jdbc-url> <superuser> <password> <service-user> <service-password>
 */
public class Main {

    public static void main(String[] args) throws Exception {
        if (args.length < 5) {
            System.err.println("Usage: Main <jdbc-url> <superuser> <super-password> <service-user> <service-password>");
            System.exit(1);
        }
        String jdbcUrl     = args[0];
        String superUser   = args[1];
        String superPass   = args[2];
        String svcUser     = args[3];
        String svcPass     = args[4];

        boolean rlsOk  = runRlsCheck(jdbcUrl, superUser, superPass, svcUser, svcPass);
        boolean embOk  = runOnnxEmbed();

        System.out.println("\n=== SUMMARY ===");
        System.out.println("RLS isolation : " + (rlsOk  ? "PASS" : "FAIL"));
        System.out.println("ONNX embed    : " + (embOk  ? "PASS" : "FAIL"));
        System.out.println("Overall       : " + (rlsOk && embOk ? "PASS" : "FAIL"));

        System.exit(rlsOk && embOk ? 0 : 1);
    }

    // ── RLS ──────────────────────────────────────────────────────────────────

    static boolean runRlsCheck(String jdbcUrl, String superUser, String superPass,
                                String svcUser, String svcPass) throws Exception {
        System.out.println("\n--- RLS ISOLATION CHECK ---");

        // Seed via superuser (bypasses RLS policies that block owner — except FORCE is on)
        // Use superuser for seeding by resetting GUC within the txn
        try (Connection su = DriverManager.getConnection(jdbcUrl, superUser, superPass)) {
            su.setAutoCommit(false);
            // Stamp tenant-A; insert three rows
            stamp(su, "tenant-A");
            insert(su, "tenant-A", "key-a1", "alpha one");
            insert(su, "tenant-A", "key-a2", "alpha two");
            insert(su, "tenant-A", "key-a3", "alpha three");
            su.commit();

            su.setAutoCommit(false);
            stamp(su, "tenant-B");
            insert(su, "tenant-B", "key-b1", "beta one");
            su.commit();
        }

        boolean pass = true;

        // Service role — tenant-A should see only A rows
        try (Connection conn = DriverManager.getConnection(jdbcUrl, svcUser, svcPass)) {
            conn.setAutoCommit(false);
            stamp(conn, "tenant-A");
            List<String> rows = queryKeys(conn);
            conn.commit();
            boolean ok = rows.size() == 3 && rows.containsAll(List.of("key-a1", "key-a2", "key-a3"));
            System.out.println("  tenant-A sees " + rows.size() + " rows: " + rows + " → " + (ok ? "PASS" : "FAIL"));
            pass &= ok;
        }

        // Service role — tenant-B should see only B rows
        try (Connection conn = DriverManager.getConnection(jdbcUrl, svcUser, svcPass)) {
            conn.setAutoCommit(false);
            stamp(conn, "tenant-B");
            List<String> rows = queryKeys(conn);
            conn.commit();
            boolean ok = rows.size() == 1 && rows.contains("key-b1");
            System.out.println("  tenant-B sees " + rows.size() + " rows: " + rows + " → " + (ok ? "PASS" : "FAIL"));
            pass &= ok;
        }

        // Service role — no GUC stamp → zero rows (fail-closed)
        try (Connection conn = DriverManager.getConnection(jdbcUrl, svcUser, svcPass)) {
            conn.setAutoCommit(false);
            // Don't stamp — unset GUC → NULL → no rows
            List<String> rows = queryKeys(conn);
            conn.commit();
            boolean ok = rows.isEmpty();
            System.out.println("  no-tenant sees " + rows.size() + " rows: " + rows + " → " + (ok ? "PASS (fail-closed)" : "FAIL (leaked!)"));
            pass &= ok;
        }

        // Cross-tenant insert blocked
        try (Connection conn = DriverManager.getConnection(jdbcUrl, svcUser, svcPass)) {
            conn.setAutoCommit(false);
            stamp(conn, "tenant-A");
            try {
                insert(conn, "tenant-B", "key-b2", "beta two cross");
                conn.commit();
                System.out.println("  cross-tenant INSERT: FAIL (not blocked!)");
                pass = false;
            } catch (Exception e) {
                conn.rollback();
                System.out.println("  cross-tenant INSERT blocked: PASS (" + e.getMessage().split("\n")[0] + ")");
            }
        }

        return pass;
    }

    private static void stamp(Connection conn, String tenant) throws Exception {
        try (var ps = conn.prepareStatement("SELECT set_config('nexus.tenant', ?, true)")) {
            ps.setString(1, tenant);
            ps.execute();
        }
    }

    private static void insert(Connection conn, String tenant, String key, String payload) throws Exception {
        DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
        ctx.execute("INSERT INTO nexus_spike.embeddings (tenant_id, key, payload) VALUES (?, ?, ?)",
                tenant, key, payload);
    }

    private static List<String> queryKeys(Connection conn) throws Exception {
        DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
        Result<Record> result = ctx.fetch("SELECT key FROM nexus_spike.embeddings ORDER BY key");
        return result.getValues("key", String.class);
    }

    // ── ONNX EMBED ───────────────────────────────────────────────────────────

    static boolean runOnnxEmbed() {
        System.out.println("\n--- ONNX EMBED CHECK ---");
        String modelDir = System.getProperty("user.home") +
                          "/.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx";
        String modelPath = modelDir + "/model.onnx";
        String tokenizerPath = modelDir + "/tokenizer.json";

        try {
            System.out.println("  Loading tokenizer: " + tokenizerPath);
            Map<String, String> tokOpts = Map.of(
                "truncation", "true",
                "maxLength",  "256",
                "padding",    "false"
            );
            HuggingFaceTokenizer tokenizer = HuggingFaceTokenizer.newInstance(
                    Path.of(tokenizerPath), tokOpts);

            System.out.println("  Loading ONNX model: " + modelPath);
            OrtEnvironment env = OrtEnvironment.getEnvironment();
            OrtSession.SessionOptions opts = new OrtSession.SessionOptions();
            OrtSession session = env.createSession(modelPath, opts);

            String text = "RDR-152 packaging probe — jlink vs native-image";
            System.out.println("  Embedding: \"" + text + "\"");

            Encoding encoding = tokenizer.encode(text);
            long[] inputIds     = encoding.getIds();
            long[] attentionMask = encoding.getAttentionMask();
            long[] tokenTypeIds  = new long[inputIds.length]; // zeros

            int seqLen = inputIds.length;
            long[] shape = {1L, seqLen};

            OnnxTensor inputIdsTensor     = OnnxTensor.createTensor(env, LongBuffer.wrap(inputIds),     shape);
            OnnxTensor attentionMaskTensor = OnnxTensor.createTensor(env, LongBuffer.wrap(attentionMask), shape);
            OnnxTensor tokenTypeIdsTensor  = OnnxTensor.createTensor(env, LongBuffer.wrap(tokenTypeIds),  shape);

            Map<String, OnnxTensor> inputs = Map.of(
                    "input_ids",      inputIdsTensor,
                    "attention_mask", attentionMaskTensor,
                    "token_type_ids", tokenTypeIdsTensor
            );

            OrtSession.Result result = session.run(inputs);
            float[][][] lastHidden = (float[][][]) result.get(0).getValue();

            // Masked mean pool
            double[] pooled = new double[384];
            double maskSum = 0;
            for (int t = 0; t < seqLen; t++) {
                float m = attentionMask[t];
                maskSum += m;
                for (int d = 0; d < 384; d++) {
                    pooled[d] += lastHidden[0][t][d] * m;
                }
            }
            double clippedSum = Math.max(maskSum, 1e-9);
            for (int d = 0; d < 384; d++) {
                pooled[d] /= clippedSum;
            }

            // L2 normalize
            double norm = 0;
            for (double v : pooled) norm += v * v;
            norm = Math.sqrt(norm);
            float[] vector = new float[384];
            for (int d = 0; d < 384; d++) {
                vector[d] = (float) (pooled[d] / norm);
            }

            // Assertions
            boolean dimOk = vector.length == 384;
            boolean finiteOk = true;
            double normCheck = 0;
            for (float v : vector) {
                if (!Float.isFinite(v)) { finiteOk = false; break; }
                normCheck += (double) v * v;
            }
            boolean normalizedOk = Math.abs(normCheck - 1.0) < 1e-5;

            System.out.printf("  Dims     : %d → %s%n", vector.length, dimOk ? "PASS" : "FAIL");
            System.out.printf("  Finite   : %s%n", finiteOk ? "PASS" : "FAIL");
            System.out.printf("  L2-norm  : %.8f → %s%n", normCheck, normalizedOk ? "PASS" : "FAIL");
            System.out.printf("  First 5  : [%.6f, %.6f, %.6f, %.6f, %.6f]%n",
                    vector[0], vector[1], vector[2], vector[3], vector[4]);

            result.close();
            session.close();
            env.close();
            tokenizer.close();

            return dimOk && finiteOk && normalizedOk;
        } catch (Exception e) {
            System.out.println("  ONNX embed FAILED: " + e);
            e.printStackTrace();
            return false;
        }
    }
}
