// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.vectors.EmbedderRouter;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
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
 *  "release_version":"0.1.6",
 *  "build_ref":"a1b2c3d+1690000000-4242",
 *  "nx_answer_steps_supported":true,
 *  "schema_latest_id":"vectors-002",
 *  "schema_changeset_count":64}</pre>
 *
 * <p>{@code app_version} comes from the JAR's own Maven {@code pom.properties}
 * and stays {@code 1.0-SNAPSHOT} — the dev coordinate, NOT the release identity
 * (RDR-002: release identity is not carried by the Maven coordinate).
 * {@code release_version} (RDR-002) is the release identity, stamped from the
 * {@code engine-service-vX.Y.Z} git tag at native-build time; it is {@code null}
 * on a dev / unstamped build so a version-pin consumer fail-closes.
 * {@code build_ref} (nexus-308ph) is a per-run artifact-identity discriminator,
 * distinct from {@code release_version}: a pinned release binary and a
 * freshly-stamped dev jar built against the SAME floor bake an identical
 * {@code release_version}, so release_version alone cannot tell them apart.
 * {@code build_ref} is stamped fresh on every gate-jar build
 * ({@code scripts/build-gate-jar.sh} / {@code tests/e2e/local-service-gate.sh})
 * as {@code <git short sha>+<per-run nonce>}; it is {@code UNSET} (the field is
 * OMITTED, never emitted as {@code null} or {@code ""}) when
 * {@code release.properties}' {@code build_ref} key is blank or absent — the
 * checked-in source default and every native-release build, so a pinned
 * release's /version body stays byte-identical in shape to before this field
 * existed. The schema
 * fields are the APPLIED Liquibase
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

    /** RDR-002: the release identity, stamped from the git tag at build time. */
    private static final String RELEASE_PROPERTIES =
            "/META-INF/nexus/release.properties";

    /** nexus-308ph: the property key for the per-run artifact-identity discriminator. */
    private static final String BUILD_REF_PROPERTY = "build_ref";

    // Liquibase's own bookkeeping table lives in `public`, outside the jOOQ codegen's
    // `nexus`/`t1` inputSchema scope (explicitly excluded — pom.xml codegen <excludes>),
    // so there is no generated Tables.DATABASECHANGELOG to reference. Typed ad-hoc
    // DSL.table/DSL.field references (nexus-mzuj9) replace the raw
    // "SELECT ... FROM public.databasechangelog" JDBC strings below — still zero
    // embedded SQL text, just no codegen for this one cross-schema table.
    private static final Table<?> DATABASECHANGELOG =
            DSL.table(DSL.name("public", "databasechangelog"));
    private static final Field<String> DBCL_ID =
            DSL.field(DSL.name("id"), String.class);
    private static final Field<Integer> DBCL_ORDER_EXECUTED =
            DSL.field(DSL.name("orderexecuted"), Integer.class);

    private final DataSource dataSource;
    private final String appVersion;
    private final String releaseVersion;   // RDR-002; null on dev / unstamped
    private final String buildRef;         // nexus-308ph; null (field OMITTED) when blank/absent
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
        this.releaseVersion = resolveReleaseVersion();
        this.buildRef = resolveBuildRef();
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

    /**
     * The stamped RDR-002 release identity, or {@code null} when this is not a
     * release build.
     *
     * <p>Reads {@code release_version} from {@link #RELEASE_PROPERTIES} (stamped
     * by the engine-service-release workflow from the git tag). A blank value, a
     * {@code SNAPSHOT}/{@code dev} qualifier, or an unreadable/absent resource all
     * map to {@code null}: an unstamped engine is, by definition, not a tagged
     * release, so a version-pin consumer (RDR-002 ez5.4) must fail closed.
     */
    static String resolveReleaseVersion() {
        try (InputStream in = VersionHandler.class.getResourceAsStream(RELEASE_PROPERTIES)) {
            if (in != null) {
                Properties props = new Properties();
                props.load(in);
                return normalizeReleaseVersion(props.getProperty("release_version"));
            }
        } catch (IOException e) {
            log.debug("event=version_release_properties_unreadable error={}", e.getMessage());
        }
        return null;
    }

    /**
     * Normalize a raw {@code release_version} property to either a release
     * identity or {@code null} (fail-closed). Blank, a {@code SNAPSHOT}/dev
     * qualifier, or {@code null} all map to {@code null}: only an explicit,
     * non-dev stamped value is a release.
     */
    static String normalizeReleaseVersion(String raw) {
        if (raw == null) {
            return null;
        }
        String v = raw.trim();
        if (v.isEmpty()) {
            return null;
        }
        // Strip a leading v/V for symmetry with the Python consumer's parser
        // (the workflow stamps without a prefix; this handles a manual set).
        if (v.startsWith("v") || v.startsWith("V")) {
            v = v.substring(1);
        }
        if (v.isEmpty()) {
            return null;
        }
        String lower = v.toLowerCase();
        if (lower.contains("snapshot") || lower.contains("dev")) {
            return null;
        }
        return v;
    }

    /**
     * The stamped nexus-308ph per-run build discriminator, or {@code null} when
     * unset — meaning the field is OMITTED from the /version body entirely
     * (never emitted as {@code null} or {@code ""}), so a pinned release built
     * before this field existed (and every native-release build, which never
     * stamps it) stays shape-identical.
     *
     * <p>Reads {@code build_ref} from {@link #RELEASE_PROPERTIES}. Unlike
     * {@link #resolveReleaseVersion()}, no {@code v}-prefix stripping or
     * {@code SNAPSHOT}/{@code dev} filtering applies — this is an opaque
     * per-run nonce (git short sha + a uniqueness suffix), not a version
     * string, so any non-blank value is significant verbatim.
     */
    static String resolveBuildRef() {
        try (InputStream in = VersionHandler.class.getResourceAsStream(RELEASE_PROPERTIES)) {
            if (in != null) {
                Properties props = new Properties();
                props.load(in);
                return normalizeBuildRef(props.getProperty(BUILD_REF_PROPERTY));
            }
        } catch (IOException e) {
            log.debug("event=version_build_ref_unreadable error={}", e.getMessage());
        }
        return null;
    }

    /**
     * Normalize a raw {@code build_ref} property to either a discriminator
     * value or {@code null} (omit the field). Blank or {@code null} both map
     * to {@code null}; any other value is returned trimmed, verbatim.
     */
    static String normalizeBuildRef(String raw) {
        if (raw == null) {
            return null;
        }
        String v = raw.trim();
        return v.isEmpty() ? null : v;
    }

    /**
     * Append the {@code build_ref} JSON field to {@code body} — but ONLY when
     * {@code buildRef} is non-null. A missing/blank build_ref means no field
     * at all (never {@code "build_ref":null}), so pre-nexus-308ph release
     * bodies and every native-release build stay byte-identical in shape.
     */
    static void appendBuildRefField(StringBuilder body, String buildRef) {
        if (buildRef != null) {
            body.append(",\"build_ref\":").append(HttpUtil.jsonString(buildRef));
        }
    }

    /**
     * RDR-196 .p1c (nexus-nyry9.9) capability advertisement: does this engine
     * accept the OPTIONAL {@code steps} array on
     * {@code POST /v1/telemetry/nx_answer_runs/record}? Unlike
     * {@code build_ref} (a nullable value, OMITTED when unset),
     * {@code nx_answer_steps_supported} is a compile-time constant on any
     * engine build carrying this handler — always present, always
     * {@code true} — so the {@code .p1d} client-side capability probe reads
     * it as "field present and true" -> steps accepted, "field absent"
     * (pre-nexus-nyry9.9 engine) -> steps not accepted, kill the client-side
     * {@code cost_usd=0.0} literals only once this reads true.
     */
    static void appendNxAnswerStepsCapabilityField(StringBuilder body) {
        body.append(",\"nx_answer_steps_supported\":true");
    }

    /** Immutable-for-the-process schema identity (nexus-hubc0). */
    private record SchemaIdentity(String latestId, long count, String error) {}

    private volatile SchemaIdentity schemaIdentity;

    /**
     * Read the changelog identity at most once per process.
     *
     * <p>Double-checked on a volatile field: a benign race can run the read
     * twice on first contact, which is harmless (it is idempotent and the
     * values are identical). Deliberately NOT synchronized — a lock here would
     * reintroduce exactly the head-of-line blocking this change removes.
     *
     * <p>A failed read is memoized too. The alternative — retrying per request —
     * would restore the unbounded pool wait on precisely the boxes where the
     * pool is already in trouble, which is the failure mode being fixed. The
     * error text still reaches the response, so the condition stays visible.
     */
    private SchemaIdentity schemaIdentity() {
        SchemaIdentity cached = schemaIdentity;
        if (cached != null) {
            return cached;
        }
        String latestId = null;
        long count = 0;
        String error = null;
        try (Connection conn = dataSource.getConnection()) {
            var dsl = DSL.using(conn, SQLDialect.POSTGRES);
            latestId = dsl.select(DBCL_ID).from(DATABASECHANGELOG)
                          .orderBy(DBCL_ORDER_EXECUTED.desc())
                          .limit(1)
                          .fetchOne(DBCL_ID);
            count = dsl.fetchCount(DATABASECHANGELOG);
        } catch (Exception e) {
            log.warn("event=version_schema_read_failed error={}", e.getMessage());
            error = e.getMessage();
        }
        SchemaIdentity fresh = new SchemaIdentity(latestId, count, error);
        schemaIdentity = fresh;
        return fresh;
    }

    @Override
    public void handle(HttpExchange exchange) throws IOException {
        if (!"GET".equalsIgnoreCase(exchange.getRequestMethod())) {
            HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}");
            return;
        }
        // nexus-hubc0: the schema identity is read ONCE and memoized, so no
        // /version request ever waits on the connection pool.
        //
        // This used to open a pooled connection per request. HikariCP's own
        // connectionTimeout is 30s, so under indexing load /version could block
        // far longer than any sane client timeout — and /version is what the
        // client's engine-version handshake and the supervisor's convergence
        // check both call (found while fixing nexus-4yf4u and nexus-7f7gb: an
        // unauthenticated probe endpoint must never contend for the pool).
        //
        // Memoizing is correct rather than merely convenient: Liquibase runs at
        // startup, before this server accepts requests, so databasechangelog is
        // immutable for the process lifetime. A migration means a new process.
        SchemaIdentity schema = schemaIdentity();
        String latestId = schema.latestId;
        long count = schema.count;
        String schemaError = schema.error;

        StringBuilder body = new StringBuilder(192);
        body.append("{\"app_version\":").append(HttpUtil.jsonString(appVersion));
        // RDR-002: the release identity, explicit null on a dev/unstamped build
        // (never silently omitted — the consumer keys its fail-closed pin on it).
        body.append(",\"release_version\":")
            .append(releaseVersion == null ? "null" : HttpUtil.jsonString(releaseVersion));
        // nexus-308ph: OMITTED (not null/empty) when unset — see appendBuildRefField.
        appendBuildRefField(body, buildRef);
        // nexus-nyry9.9: always present, always true on any engine carrying this handler.
        appendNxAnswerStepsCapabilityField(body);
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
