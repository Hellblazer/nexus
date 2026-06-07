// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.net.ServerSocket;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * RDR-152 bead nexus-gmiaf.20 — Java-managed local Chroma HTTP server.
 *
 * <p>DECISION (user, 2026-06-07): Option A — Java service spawns and supervises the
 * local {@code chroma run} process.  {@link #start()} blocks until the server is
 * reachable (heartbeat poll). {@link #stop()} sends SIGTERM and waits for the child
 * to exit; SIGKILL after a 5 s grace period.
 *
 * <p>Data compatibility: {@code chroma run --path <dir>} serves the SAME on-disk
 * format as {@code chromadb.PersistentClient(path=<dir>)}.  Zero data loss on
 * transition (confirmed by reconnaissance test 2026-06-07).
 *
 * <p>Phase 5 (.30) follow-on: auto-restart on child exit / health-check failures.
 * In .20, a crashed child is logged but not restarted — the service continues
 * serving Postgres-backed operations; vector ops fail until the next service restart.
 */
public final class LocalChromaServer implements AutoCloseable {

    private static final Logger log = LoggerFactory.getLogger(LocalChromaServer.class);

    /** Grace period before SIGKILL on shutdown. */
    private static final long STOP_GRACE_SECONDS = 5L;

    /** How long to wait for the server to become ready (heartbeat poll). */
    private static final long STARTUP_TIMEOUT_MS = 20_000L;

    /** Poll interval during startup. */
    private static final long STARTUP_POLL_MS = 250L;

    private final String  chromaBinary;
    private final String  dataPath;
    private final int     port;

    private volatile Process chromaProcess;
    private volatile boolean started = false;
    /** Guards one-time JVM shutdown hook registration (re-start / test-suite safety). */
    private final AtomicBoolean shutdownHookRegistered = new AtomicBoolean(false);

    /**
     * @param chromaBinary path to the {@code chroma} CLI (e.g. from venv or PATH)
     * @param dataPath     path to the ChromaDB data directory (same as PersistentClient path)
     * @param port         loopback port to listen on; use {@link #findFreePort()} if 0
     */
    public LocalChromaServer(String chromaBinary, String dataPath, int port) {
        this.chromaBinary = chromaBinary;
        this.dataPath     = dataPath;
        this.port         = port;
    }

    /**
     * Start the local Chroma server.  Blocks until the heartbeat is reachable or
     * {@code STARTUP_TIMEOUT_MS} elapses.
     *
     * @throws IOException if the process cannot be started
     * @throws RuntimeException if the server does not become ready in time
     */
    public void start() throws IOException {
        // Ensure data directory exists
        Files.createDirectories(Path.of(dataPath));

        List<String> cmd = new ArrayList<>(List.of(
                chromaBinary, "run",
                "--path", dataPath,
                "--host", "127.0.0.1",
                "--port", String.valueOf(port)
        ));

        log.info("event=local_chroma_starting binary={} path={} port={}", chromaBinary, dataPath, port);

        ProcessBuilder pb = new ProcessBuilder(cmd)
                .redirectErrorStream(true)
                .inheritIO();   // Route chroma stdout/stderr to service stdout for visibility
        chromaProcess = pb.start();

        started = true;
        log.info("event=local_chroma_process_spawned pid={}", chromaProcess.pid());

        // Register shutdown hook ONCE per instance (guards auto-restart in Phase .30
        // and test-suite scenarios where start() may be called more than once).
        if (shutdownHookRegistered.compareAndSet(false, true)) {
            Runtime.getRuntime().addShutdownHook(new Thread(this::forceStop, "chroma-shutdown-hook"));
        }

        // Poll heartbeat until ready or timeout
        ChromaRestClient probe = ChromaRestClient.local("127.0.0.1", port);
        long deadline = System.currentTimeMillis() + STARTUP_TIMEOUT_MS;
        boolean ready = false;
        while (System.currentTimeMillis() < deadline) {
            if (!chromaProcess.isAlive()) {
                throw new RuntimeException(
                        "Chroma process exited prematurely during startup (exit=" +
                        chromaProcess.exitValue() + ")");
            }
            if (probe.heartbeat()) {
                ready = true;
                break;
            }
            try { Thread.sleep(STARTUP_POLL_MS); } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new RuntimeException("interrupted waiting for Chroma startup", e);
            }
        }
        if (!ready) {
            forceStop();
            throw new RuntimeException(
                    "Local Chroma server did not become ready within " +
                    STARTUP_TIMEOUT_MS + " ms on port " + port);
        }
        log.info("event=local_chroma_ready port={} pid={}", port, chromaProcess.pid());
    }

    /**
     * Graceful stop: SIGTERM → wait 5 s → SIGKILL.
     */
    public void stop() {
        Process p = chromaProcess;
        if (p == null || !p.isAlive()) return;
        log.info("event=local_chroma_stopping pid={}", p.pid());
        p.destroy(); // SIGTERM
        try {
            boolean exited = p.waitFor(STOP_GRACE_SECONDS, TimeUnit.SECONDS);
            if (!exited) {
                log.warn("event=local_chroma_sigkill pid={}", p.pid());
                p.destroyForcibly();
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            p.destroyForcibly();
        }
        log.info("event=local_chroma_stopped");
    }

    private void forceStop() {
        Process p = chromaProcess;
        if (p != null && p.isAlive()) {
            p.destroyForcibly();
        }
    }

    @Override
    public void close() {
        stop();
    }

    /** Return the port this server is listening on. */
    public int getPort() { return port; }

    /** Return true if the process was started and is still alive. */
    public boolean isAlive() {
        return started && chromaProcess != null && chromaProcess.isAlive();
    }

    /**
     * Find a free loopback port by binding a server socket to port 0 and reading
     * the assigned port, then immediately closing the socket.
     *
     * <p>There is a small race window between closing and the Chroma process binding
     * the port, but it is negligible on a loopback interface in a test context.
     */
    public static int findFreePort() {
        try (ServerSocket s = new ServerSocket(0)) {
            return s.getLocalPort();
        } catch (IOException e) {
            throw new RuntimeException("Cannot find a free port", e);
        }
    }

    /**
     * Attempt to locate the {@code chroma} CLI binary.  Searches:
     * <ol>
     *   <li>{@code NX_CHROMA_BINARY} env var</li>
     *   <li>{@code $NX_VENV_PATH/bin/chroma} (if NX_VENV_PATH is set)</li>
     *   <li>Common venv location: {@code ~/.config/nexus/venv/bin/chroma}</li>
     *   <li>Next to the running JVM: looks for {@code chroma} in {@code ../venv/bin/}</li>
     *   <li>PATH via {@code which chroma}</li>
     * </ol>
     *
     * @return the path to the chroma binary
     * @throws RuntimeException if not found
     */
    public static String findChromaBinary() {
        // 1. Explicit override
        String explicit = System.getenv("NX_CHROMA_BINARY");
        if (explicit != null && !explicit.isBlank() && java.nio.file.Files.isExecutable(Path.of(explicit))) {
            return explicit;
        }

        // 2. NX_VENV_PATH
        String venvPath = System.getenv("NX_VENV_PATH");
        if (venvPath != null && !venvPath.isBlank()) {
            Path candidate = Path.of(venvPath, "bin", "chroma");
            if (Files.isExecutable(candidate)) return candidate.toString();
        }

        // 3. Common nexus venv location
        Path common = Path.of(System.getProperty("user.home"), ".config", "nexus", "venv", "bin", "chroma");
        if (Files.isExecutable(common)) return common.toString();

        // 4. Try "which chroma" via shell
        try {
            Process which = new ProcessBuilder("which", "chroma")
                    .redirectErrorStream(true).start();
            boolean done = which.waitFor(3, TimeUnit.SECONDS);
            if (done && which.exitValue() == 0) {
                String found = new String(which.getInputStream().readAllBytes()).trim();
                if (!found.isEmpty() && Files.isExecutable(Path.of(found))) return found;
            }
        } catch (Exception ignored) {}

        throw new RuntimeException(
                "Cannot locate 'chroma' binary. Set NX_CHROMA_BINARY env var to the " +
                "path of the chromadb CLI (usually <venv>/bin/chroma).");
    }
}
