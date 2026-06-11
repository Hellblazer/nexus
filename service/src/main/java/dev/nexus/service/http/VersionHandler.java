// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.vectors.EmbedderRouter;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import javax.sql.DataSource;
import java.io.IOException;
import java.io.InputStream;
import java.sql.Connection;
import java.util.Properties;

/**
 * GET /version — app + schema version handshake (bead nexus-pebfx.4).
 * No authentication required (mirrors /health; loopback-only service,
 * version metadata crosses no trust boundary).
 *
 * <p>Returns 200:
 * <pre>{"app_version":"1.0-SNAPSHOT",
 *  "schema_latest_id":"vectors-002",
 *  "schema_changeset_count":64}</pre>
 *
 * <p>{@code app_version} comes from the JAR's own Maven
 * {@code pom.properties}; the schema fields are the APPLIED Liquibase
 * journal (the service ran {@code update} at startup, so applied ==
 * bundled for a healthy instance). On a journal read failure the schema
 * fields are {@code null} and {@code schema_error} carries the cause —
 * explicitly reported, never silently omitted.
 *
 * <p>nx clients use this to (a) display the running service version in
 * {@code nx daemon service status}, (b) warn when the RUNNING JAR differs
 * from the JAR installed at the well-known location (stale service), and
 * (c) the supervisor refuses pre-spawn when the JAR-to-start is older than
 * the applied journal (the Python-side gate; Liquibase itself silently
 * ignores unknown applied changesets).
 */
public final class VersionHandler implements HttpHandler {

    private static final Logger log = LoggerFactory.getLogger(VersionHandler.class);

    private static final String POM_PROPERTIES =
            "/META-INF/maven/dev.nexus/nexus-service/pom.properties";

    private final DataSource dataSource;
    private final String appVersion;
    private final EmbedderRouter embedderRouter;   // nullable — mode "unknown"

    public VersionHandler(DataSource dataSource) {
        this(dataSource, null);
    }

    /**
     * @param embedderRouter the doc-side router; supplies the
     *        nexus-pebfx.5 embedding-mode handshake fields
     *        ({@code embedding_mode}, {@code embedding_models}) so
     *        {@code nx daemon service status} can show voyage|onnx-local
     *        without parsing DEVNULLed JAR logs. Null → "unknown".
     */
    public VersionHandler(DataSource dataSource, EmbedderRouter embedderRouter) {
        this.dataSource = dataSource;
        this.embedderRouter = embedderRouter;
        this.appVersion = resolveAppVersion();
    }

    /** Maven pom.properties (fat JAR) → Implementation-Version → "unknown". */
    static String resolveAppVersion() {
        try (InputStream in = VersionHandler.class.getResourceAsStream(POM_PROPERTIES)) {
            if (in != null) {
                Properties props = new Properties();
                props.load(in);
                String v = props.getProperty("version");
                if (v != null && !v.isBlank()) {
                    return v;
                }
            }
        } catch (IOException e) {
            log.debug("event=version_pom_properties_unreadable error={}", e.getMessage());
        }
        String impl = VersionHandler.class.getPackage().getImplementationVersion();
        return impl != null ? impl : "unknown";
    }

    @Override
    public void handle(HttpExchange exchange) throws IOException {
        if (!"GET".equalsIgnoreCase(exchange.getRequestMethod())) {
            HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}");
            return;
        }
        String latestId = null;
        long count = 0;
        String schemaError = null;
        try (Connection conn = dataSource.getConnection();
             var stmt = conn.createStatement()) {
            try (var rs = stmt.executeQuery(
                    "SELECT id FROM public.databasechangelog "
                    + "ORDER BY orderexecuted DESC LIMIT 1")) {
                if (rs.next()) {
                    latestId = rs.getString(1);
                }
            }
            try (var rs = stmt.executeQuery(
                    "SELECT count(*) FROM public.databasechangelog")) {
                if (rs.next()) {
                    count = rs.getLong(1);
                }
            }
        } catch (Exception e) {
            log.warn("event=version_schema_read_failed error={}", e.getMessage());
            schemaError = e.getMessage();
        }

        StringBuilder body = new StringBuilder(192);
        body.append("{\"app_version\":").append(HttpUtil.jsonString(appVersion));
        if (embedderRouter != null) {
            body.append(",\"embedding_mode\":")
                .append(HttpUtil.jsonString(embedderRouter.modeName()))
                .append(",\"embedding_models\":[");
            var models = embedderRouter.availableModels();
            for (int i = 0; i < models.size(); i++) {
                if (i > 0) body.append(',');
                body.append(HttpUtil.jsonString(models.get(i)));
            }
            body.append(']');
        } else {
            body.append(",\"embedding_mode\":\"unknown\"");
        }
        if (schemaError == null) {
            body.append(",\"schema_latest_id\":")
                .append(latestId == null ? "null" : HttpUtil.jsonString(latestId))
                .append(",\"schema_changeset_count\":").append(count);
        } else {
            body.append(",\"schema_latest_id\":null")
                .append(",\"schema_changeset_count\":null")
                .append(",\"schema_error\":").append(HttpUtil.jsonString(schemaError));
        }
        body.append('}');
        HttpUtil.send(exchange, 200, body.toString());
    }
}
