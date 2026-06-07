package dev.nexus.service.http;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.db.TenantScope;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import javax.sql.DataSource;
import java.io.IOException;
import java.sql.Connection;

/**
 * GET /health — liveness + DB probe. No authentication required.
 *
 * Returns 200 {"status":"ok","db":"up"} on success,
 *         503 {"status":"error","db":"down","detail":"..."} on DB failure.
 */
public final class HealthHandler implements HttpHandler {

    private static final Logger log = LoggerFactory.getLogger(HealthHandler.class);

    private final DataSource dataSource;

    public HealthHandler(DataSource dataSource) {
        this.dataSource = dataSource;
    }

    @Override
    public void handle(HttpExchange exchange) throws IOException {
        if (!"GET".equalsIgnoreCase(exchange.getRequestMethod())) {
            HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}");
            return;
        }
        try {
            checkDb();
            HttpUtil.send(exchange, 200, "{\"status\":\"ok\",\"db\":\"up\"}");
        } catch (Exception e) {
            log.warn("event=health_db_down error={}", e.getMessage());
            String body = "{\"status\":\"error\",\"db\":\"down\",\"detail\":" +
                          HttpUtil.jsonString(e.getMessage()) + "}";
            HttpUtil.send(exchange, 503, body);
        }
    }

    private void checkDb() throws Exception {
        try (Connection conn = dataSource.getConnection();
             var stmt = conn.createStatement();
             var rs = stmt.executeQuery("SELECT 1")) {
            if (!rs.next()) {
                throw new RuntimeException("SELECT 1 returned no rows");
            }
        }
    }
}
