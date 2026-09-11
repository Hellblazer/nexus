// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.tuples;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.io.InputStream;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * RDR-205 §Technical Design "Registry": loads, validates, and serves the
 * tuple-space template registry (bead nexus-em75s.3). The engine is the
 * only holder of this registry — no client carries a copy, every write is
 * validated engine-side against these templates, and a client learns what
 * exists by asking {@link #registry()}.
 *
 * <p><b>Sources.</b> {@value #SOURCE_RESOURCES}: the classpath resources
 * shipped with every build, loaded by explicit name (native image has no
 * classpath directory enumeration — the {@code VersionHandler} precedent).
 * An optional test-only filesystem directory named by {@value
 * #TEMPLATE_DIR_ENV} is a second source, logged at boot and listed in
 * {@link #registry()} so the cloud client-path gate can assert a deployed
 * engine reports {@value #SOURCE_RESOURCES} alone.
 *
 * <p><b>Boot check.</b> The claim log's TTL setting ({@value
 * #CLAIM_LOG_TTL_DAYS_ENV}, default {@value #DEFAULT_CLAIM_LOG_TTL_DAYS}
 * days) must exceed every loaded template's {@code retention_seconds} by
 * strictly more than one sweep interval — equality refuses to boot, naming
 * the template, so the purge order in the RDR's §Indexes and hygiene is
 * checked for every row rather than assumed.
 *
 * <p><b>YAML, no new dependency (design call).</b> {@code service/pom.xml}
 * carries no YAML parser; adding {@code jackson-dataformat-yaml} would pull
 * native-image reflection config for every mapped POJO plus a resource
 * include pattern, for exactly two fixed, flat templates. This module
 * parses the RDR-205 document shape with a hand-written subset parser
 * ({@link MiniYaml}) instead. The already-present {@code jackson-databind}
 * is still used, but only to serialize a plain {@code Map}/{@code List}
 * tree for the registry digest — no POJO reflection, so no new
 * native-image config is needed for that (the same pattern {@code
 * TelemetryHandler} already round-trips through a native build).
 */
public final class TemplateRegistry {

    private static final Logger log = LoggerFactory.getLogger(TemplateRegistry.class);

    public static final String TEMPLATE_DIR_ENV = "NX_TUPLE_TEMPLATE_DIR";
    public static final String CLAIM_LOG_TTL_DAYS_ENV = "NX_TUPLE_CLAIM_LOG_TTL_DAYS";
    public static final long DEFAULT_CLAIM_LOG_TTL_DAYS = 180L;

    /** The {@link #sources()} label for the shipped classpath resources. */
    public static final String SOURCE_RESOURCES = "resources";

    /**
     * The two v1 templates, and only two (RDR-205). Loaded by explicit name —
     * never by classpath directory enumeration, which native image has none of.
     */
    private static final List<String> RESOURCE_TEMPLATE_PATHS = List.of(
            "/tuples/templates/ledger.yaml",
            "/tuples/templates/mailbox.yaml");

    private static final ObjectMapper DIGEST_MAPPER = new ObjectMapper();

    private final List<TemplateSchema> templates;
    private final List<String> sources;
    private final String digest;
    private final long claimLogTtlSeconds;

    private TemplateRegistry(List<TemplateSchema> templates, List<String> sources, String digest,
                              long claimLogTtlSeconds) {
        this.templates = templates;
        this.sources = sources;
        this.digest = digest;
        this.claimLogTtlSeconds = claimLogTtlSeconds;
    }

    public List<TemplateSchema> templates() {
        return templates;
    }

    /** Every source this registry was loaded from, in load order (e.g. {@code ["resources"]}). */
    public List<String> sources() {
        return sources;
    }

    public String digest() {
        return digest;
    }

    /**
     * The claim log's TTL, in seconds ({@value #CLAIM_LOG_TTL_DAYS_ENV}, parsed once at
     * boot). RDR-205 Phase 1 Step 5 (bead nexus-em75s.5): the sweep's log-purge arm reads
     * this rather than re-parsing {@value #CLAIM_LOG_TTL_DAYS_ENV} itself — the value this
     * registry's own boot check already validated against every template's {@code
     * retention_seconds} is the value the sweep purges by; a second parse of the same env
     * var would risk drifting from what boot actually enforced.
     */
    public long claimLogTtlSeconds() {
        return claimLogTtlSeconds;
    }

    /** {@code registry() -> {digest, templates}} (RDR-205 §Technical Design), plus {@link #sources()}. */
    public Snapshot registry() {
        return new Snapshot(digest, sources, templates);
    }

    public record Snapshot(String digest, List<String> sources, List<TemplateSchema> templates) {
    }

    /**
     * Literal-before-template resolution — the May load rule RDR-205 carries
     * verbatim: "a literal name is looked up before templates". Returns
     * {@code null} when nothing matches.
     */
    public TemplateSchema resolve(String subspace) {
        List<String> input = List.of(subspace.split("/", -1));
        for (TemplateSchema t : templates) {
            if (t.isLiteral() && t.nameSegments().equals(input)) {
                return t;
            }
        }
        for (TemplateSchema t : templates) {
            if (!t.isLiteral() && matchesTemplate(t.nameSegments(), input)) {
                return t;
            }
        }
        return null;
    }

    private static boolean matchesTemplate(List<String> templateSegments, List<String> input) {
        if (templateSegments.size() != input.size()) {
            return false;
        }
        for (int i = 0; i < templateSegments.size(); i++) {
            String ts = templateSegments.get(i);
            if (TemplateSchema.isParamSegment(ts)) {
                continue;
            }
            if (!ts.equals(input.get(i))) {
                return false;
            }
        }
        return true;
    }

    // ── boot entry point ────────────────────────────────────────────────

    /** Production boot call: reads {@value #TEMPLATE_DIR_ENV} and {@value #CLAIM_LOG_TTL_DAYS_ENV}
     *  via {@code System.getenv} directly. {@code sweepIntervalSeconds} is caller-supplied
     *  (constructor-injection style) — see {@code NexusService.SWEEP_INTERVAL_HOURS}. */
    public static TemplateRegistry loadAtBoot(long sweepIntervalSeconds) {
        return loadAtBoot(System.getenv(TEMPLATE_DIR_ENV), System.getenv(CLAIM_LOG_TTL_DAYS_ENV), sweepIntervalSeconds);
    }

    /** Testable form (no {@code System.getenv} reads) — mirrors this module's
     *  {@code resolveBindHost}/{@code intEnv} shape. */
    public static TemplateRegistry loadAtBoot(String templateDirEnv, String claimLogTtlDaysEnv, long sweepIntervalSeconds) {
        List<SourceGroup> groups = new ArrayList<>();
        groups.add(new SourceGroup(SOURCE_RESOURCES, loadResourceSources()));

        if (templateDirEnv != null && !templateDirEnv.isBlank()) {
            Path dir = Path.of(templateDirEnv.trim());
            String label = "directory:" + dir;
            log.info("event=tuple_template_dir_loaded path={}", dir);
            groups.add(new SourceGroup(label, loadDirectorySources(dir)));
        }

        long claimLogTtlDays = parseClaimLogTtlDays(claimLogTtlDaysEnv);
        return load(groups, claimLogTtlDays * 86_400L, sweepIntervalSeconds);
    }

    private static long parseClaimLogTtlDays(String raw) {
        if (raw == null || raw.isBlank()) {
            return DEFAULT_CLAIM_LOG_TTL_DAYS;
        }
        long days;
        try {
            days = Long.parseLong(raw.trim());
        } catch (NumberFormatException e) {
            throw new TemplateRegistryException(CLAIM_LOG_TTL_DAYS_ENV, null,
                    "must be an integer number of days (got '" + raw + "')");
        }
        if (days <= 0) {
            throw new TemplateRegistryException(CLAIM_LOG_TTL_DAYS_ENV, null,
                    "must be a positive integer number of days (got " + days + ")");
        }
        return days;
    }

    private static List<TemplateSource> loadResourceSources() {
        List<TemplateSource> out = new ArrayList<>();
        for (String path : RESOURCE_TEMPLATE_PATHS) {
            try (InputStream in = TemplateRegistry.class.getResourceAsStream(path)) {
                if (in == null) {
                    throw new TemplateRegistryException(path, null, "resource not found on the classpath");
                }
                String text = new String(in.readAllBytes(), StandardCharsets.UTF_8);
                out.add(new TemplateSource(path, text));
            } catch (IOException e) {
                throw new UncheckedIOException("failed to read template resource " + path, e);
            }
        }
        return out;
    }

    private static List<TemplateSource> loadDirectorySources(Path dir) {
        if (!Files.isDirectory(dir)) {
            throw new TemplateRegistryException(TEMPLATE_DIR_ENV, null,
                    "is set to '" + dir + "', which is not a directory");
        }
        List<Path> files = new ArrayList<>();
        try (DirectoryStream<Path> stream = Files.newDirectoryStream(dir, "*.yaml")) {
            for (Path p : stream) {
                files.add(p);
            }
        } catch (IOException e) {
            throw new UncheckedIOException("failed to list " + TEMPLATE_DIR_ENV + " directory " + dir, e);
        }
        files.sort(Comparator.comparing(Path::getFileName));
        List<TemplateSource> out = new ArrayList<>();
        for (Path p : files) {
            try {
                out.add(new TemplateSource(p.toString(), Files.readString(p, StandardCharsets.UTF_8)));
            } catch (IOException e) {
                throw new UncheckedIOException("failed to read " + p, e);
            }
        }
        return out;
    }

    // ── pure core (fully testable, no filesystem or classpath) ─────────

    record TemplateSource(String descriptor, String yamlText) {
    }

    record SourceGroup(String label, List<TemplateSource> files) {
    }

    static TemplateRegistry load(List<SourceGroup> groups, long claimLogTtlSeconds, long sweepIntervalSeconds) {
        Map<String, TemplateSchema> byName = new LinkedHashMap<>();
        Map<String, String> firstSourceOf = new LinkedHashMap<>();
        List<String> sources = new ArrayList<>();

        for (SourceGroup group : groups) {
            sources.add(group.label());
            for (TemplateSource file : group.files()) {
                Map<String, Object> doc = MiniYaml.parse(file.descriptor(), file.yamlText());
                TemplateSchema schema = TemplateSchemaParser.parse(file.descriptor(), doc);
                if (byName.containsKey(schema.name())) {
                    throw new TemplateRegistryException(file.descriptor(), "name",
                            "duplicate template name '" + schema.name() + "' (first defined in "
                                    + firstSourceOf.get(schema.name()) + ")");
                }
                byName.put(schema.name(), schema);
                firstSourceOf.put(schema.name(), file.descriptor());
            }
        }

        List<TemplateSchema> templates = byName.values().stream()
                .sorted(Comparator.comparing(TemplateSchema::name))
                .toList();

        for (TemplateSchema t : templates) {
            long ceiling = t.retentionSeconds() + sweepIntervalSeconds;
            if (claimLogTtlSeconds <= ceiling) {
                throw new TemplateRegistryException(t.name(), "retention_seconds",
                        "refuses to boot: claim log TTL " + claimLogTtlSeconds + "s ("
                                + CLAIM_LOG_TTL_DAYS_ENV + ") does not exceed template '" + t.name()
                                + "' retention_seconds " + t.retentionSeconds()
                                + "s by strictly more than one sweep interval " + sweepIntervalSeconds + "s");
            }
        }

        String digest = computeDigest(templates);
        log.info("event=tuple_template_registry_loaded sources={} templates={} digest={}",
                sources, templates.size(), digest);
        return new TemplateRegistry(templates, List.copyOf(sources), digest, claimLogTtlSeconds);
    }

    private static String computeDigest(List<TemplateSchema> templates) {
        List<Map<String, Object>> canonical = templates.stream().map(TemplateRegistry::toCanonicalMap).toList();
        try {
            byte[] json = DIGEST_MAPPER.writeValueAsBytes(canonical);
            byte[] hash = MessageDigest.getInstance("SHA-256").digest(json);
            return HexFormat.of().formatHex(hash);
        } catch (NoSuchAlgorithmException | JsonProcessingException e) {
            throw new IllegalStateException("failed to compute template registry digest", e);
        }
    }

    private static Map<String, Object> toCanonicalMap(TemplateSchema t) {
        Map<String, Object> m = new TreeMap<>();
        m.put("name", t.name());
        m.put("keys", t.keys());
        if (!t.keyValues().isEmpty()) {
            m.put("key_values", new TreeMap<>(t.keyValues()));
        }
        Map<String, Object> dims = new TreeMap<>();
        for (Map.Entry<String, TemplateSchema.Dimension> e : t.dimensions().entrySet()) {
            Map<String, Object> d = new TreeMap<>();
            d.put("type", e.getValue().type());
            if (e.getValue().values() != null) {
                d.put("values", e.getValue().values());
            }
            d.put("required", e.getValue().required());
            dims.put(e.getKey(), d);
        }
        m.put("dimensions", dims);
        m.put("id_from", t.idFrom().wire());
        if (!t.idDims().isEmpty()) {
            m.put("id_dims", t.idDims());
        }
        Map<String, Object> take = new TreeMap<>();
        take.put("enabled", t.take().enabled());
        if (t.take().defaultLeaseSeconds() != null) {
            take.put("default_lease_seconds", t.take().defaultLeaseSeconds());
        }
        if (t.take().maxLeaseSeconds() != null) {
            take.put("max_lease_seconds", t.take().maxLeaseSeconds());
        }
        if (t.take().maxAttempts() != null) {
            take.put("max_attempts", t.take().maxAttempts());
        }
        m.put("take", take);
        m.put("retention_seconds", t.retentionSeconds());
        return m;
    }
}
