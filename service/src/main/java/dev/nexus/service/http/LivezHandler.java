package dev.nexus.service.http;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;

import java.io.IOException;

/**
 * GET /livez — LIVENESS only. No authentication, and deliberately NO dependency
 * of any kind: no database, no pool, no I/O beyond the socket.
 *
 * <p>nexus-hubc0 / nexus-7f7gb (GH #1419 issue 3b). {@link HealthHandler} is a
 * READINESS probe — it takes a HikariCP connection and runs {@code SELECT 1}, so
 * it answers "can I serve requests right now". The supervisor was using it as
 * the RESTART trigger, which made the probe that decides whether to kill the
 * service compete for the exact resource that saturation exhausts. Under
 * CPU-bound indexing the pool is contended, {@code /health} blocks past the
 * probe timeout, and a perfectly healthy service gets cycled mid-batch —
 * severing in-flight clients, which retry, which adds load. Self-amplifying.
 *
 * <p>The supervisor's probe budget could not simply be widened: it blocks the
 * heartbeat thread, so the lease TTL caps it (a 20s probe made the supervisor
 * lose its own lease mid-probe — a vanished endpoint instead of a spurious
 * restart). The way out is a liveness signal that cannot be slowed by load.
 *
 * <p>So the split is the standard one, and the distinction is load-bearing
 * rather than cosmetic:
 * <ul>
 *   <li>{@code /livez}  — is the process alive and its HTTP loop responsive?
 *       Restart authority. Cannot be made slow by a busy pool, a slow query,
 *       or a down database, because it touches none of them.</li>
 *   <li>{@code /health} — is the process able to serve, dependencies included?
 *       Readiness: lease-stamping, {@code nx doctor}, load balancers.</li>
 * </ul>
 *
 * <p>A 503 from {@code /health} therefore means "alive but not serving" and must
 * never trigger a restart; only silence from {@code /livez} justifies one.
 *
 * <p>Deliberately allocation-free and branch-free past the method check: any
 * work added here weakens the guarantee that a saturated process can still
 * answer it.
 */
public final class LivezHandler implements HttpHandler {

    private static final String BODY = "{\"status\":\"ok\"}";

    @Override
    public void handle(HttpExchange exchange) throws IOException {
        if (!"GET".equalsIgnoreCase(exchange.getRequestMethod())) {
            HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}");
            return;
        }
        HttpUtil.send(exchange, 200, BODY);
    }
}
