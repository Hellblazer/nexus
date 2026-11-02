// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.tuples;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.SchemaViolationException;
import dev.nexus.service.db.TooLargeException;
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
import java.util.HashSet;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.regex.Pattern;

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
 * <p><b>Boot check.</b> Every loaded template's EFFECTIVE claim-log TTL — its own
 * {@link TemplateSchema#claimLogTtlSeconds()} when it declares one (RDR-211 Scale
 * and Limits item 6, bead nexus-rplay.6), else the engine-wide {@value
 * #CLAIM_LOG_TTL_DAYS_ENV} default (default {@value #DEFAULT_CLAIM_LOG_TTL_DAYS}
 * days) — must exceed that SAME template's own {@code retention_seconds} by
 * strictly more than one sweep interval — equality refuses to boot, naming
 * the template, so the purge order in the RDR's §Indexes and hygiene is
 * checked for every row rather than assumed. A template's own override may only
 * SHORTEN the engine default, never lengthen it; a longer one also refuses to
 * boot, naming the template.
 *
 * <p><b>YAML, no new dependency (design call).</b> {@code service/pom.xml}
 * carries no YAML parser; adding {@code jackson-dataformat-yaml} would pull
 * native-image reflection config for every mapped POJO plus a resource
 * include pattern, for a small, fixed set of flat templates. This module
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
     * The shipped v1 templates (RDR-205, plus {@code directory/<name>} from
     * RDR-208 Phase 1 Step 1, bead nexus-galkv.1, and {@code board/<topic>},
     * {@code lock/<resource>}, {@code queue/<name>} from RDR-211 Phase 1
     * Step 2, bead nexus-rplay.8, and the literal one-segment {@code aspects}
     * from RDR-235 E3, bead nexus-5r2sc.2.4; the RDR-205 {@code ledger/<session_id>}
     * template was retired by bead nexus-onjvw). Loaded by explicit name — never by
     * classpath directory enumeration, which native image has none of. List
     * order here does not determine {@link #templates()} order — {@link
     * #load} sorts the final list by name — but is still the load order used
     * for "first defined in" duplicate-name diagnostics.
     *
     * <p>Package-private so {@code TemplateDocLintTest} (RDR-239 Phase 3, bead nexus-mdbds.2.19)
     * can check that this hand-kept list and the templates directory name the same files.
     */
    static final List<String> RESOURCE_TEMPLATE_PATHS = List.of(
            "/tuples/templates/aspects.yaml",
            "/tuples/templates/board.yaml",
            "/tuples/templates/board-ci.yaml",
            "/tuples/templates/directory.yaml",
            "/tuples/templates/lock.yaml",
            "/tuples/templates/mailbox.yaml",
            "/tuples/templates/queue.yaml");

    private static final ObjectMapper DIGEST_MAPPER = new ObjectMapper();

    private final List<TemplateSchema> templates;
    private final Map<String, TemplateSchema> byName;
    private final List<String> sources;
    private final String digest;
    private final long claimLogTtlSeconds;

    private TemplateRegistry(List<TemplateSchema> templates, List<String> sources, String digest,
                              long claimLogTtlSeconds) {
        this.templates = templates;
        // Safe unqualified lookup: load() already refused a duplicate name before this
        // constructor ever runs, so every template's own name is a unique map key here.
        Map<String, TemplateSchema> names = new LinkedHashMap<>();
        for (TemplateSchema t : templates) {
            names.put(t.name(), t);
        }
        this.byName = Map.copyOf(names);
        this.sources = sources;
        this.digest = digest;
        this.claimLogTtlSeconds = claimLogTtlSeconds;
    }

    public List<TemplateSchema> templates() {
        return templates;
    }

    /** The template whose declared {@link TemplateSchema#name()} is exactly {@code name},
     *  or {@code null} if none loaded under that name. Unlike {@link #resolve}, this is an
     *  exact key lookup against the registry's OWN name strings, never pattern-matched
     *  against a concrete subspace address -- what {@code nexus.tuples.template} already
     *  stores verbatim (RDR-211, bead nexus-rplay.6: {@code TupleRepository#insertClaimLog}
     *  uses this to find a row's per-template claim-log TTL, if any). */
    public TemplateSchema byName(String name) {
        return byName.get(name);
    }

    /**
     * RDR-211 Scale and Limits item 6 ("claim-log volume"): the claim-log TTL that
     * actually governs a specific template's log rows -- its own {@link
     * TemplateSchema#claimLogTtlSeconds()} when it declares one, else this registry's
     * engine-wide {@link #claimLogTtlSeconds()}. {@code templateName} not found (a row
     * from a template the registry no longer carries) falls back to the engine default,
     * the same posture {@link #resolve} takes for an unmatched subspace elsewhere in this
     * class -- never a thrown exception from what is, at this call site, a write already
     * past every other validation.
     */
    public long effectiveClaimLogTtlSeconds(String templateName) {
        TemplateSchema t = byName.get(templateName);
        return t == null ? claimLogTtlSeconds : effectiveClaimLogTtlSeconds(t, claimLogTtlSeconds);
    }

    /** The pure form of {@link #effectiveClaimLogTtlSeconds(String)}, usable in {@link
     *  #load} before a {@link TemplateRegistry} instance exists to call it on. */
    private static long effectiveClaimLogTtlSeconds(TemplateSchema t, long registryDefaultSeconds) {
        return t.claimLogTtlSeconds() != null ? t.claimLogTtlSeconds() : registryDefaultSeconds;
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
     * The tuple-space address grammar (nexus-mvfm9, RDR-205 follow-on): a
     * {@code <param>} segment of a template name — the address portion of a
     * subspace like {@code mailbox/<address>} — must be non-empty and match
     * this shape. Before this fix {@link #matchesTemplate} accepted ANY
     * segment count match regardless of the param segment's content,
     * including the empty string ({@code "mailbox/"} splits to
     * {@code ["mailbox", ""]}, two segments, matching {@code ["mailbox",
     * "<address>"]}) and arbitrary bytes ({@code "mailbox/bad name!"} —
     * space and {@code !} are neither escaped nor rejected). Every real
     * address in this codebase (claimant ids, session ids, instance names —
     * see {@code conexus/skills/mailbox/SKILL.md}, the mailbox test
     * fixtures) is a bare identifier: letters, digits, {@code .}, {@code _},
     * {@code -}. A subspace whose address fails this grammar now simply
     * fails to match ANY template, so {@link #resolve} returns {@code null}
     * and every caller's {@code resolveOrThrow} raises
     * {@code UnknownSubspaceException} uniformly — the same failure mode as
     * a template name that does not exist at all, deliberately not a new
     * error type.
     */
    static final Pattern ADDRESS_SEGMENT_PATTERN = Pattern.compile("[A-Za-z0-9][A-Za-z0-9._-]*");

    /**
     * Literal-before-template resolution — the May load rule RDR-205 carries
     * verbatim: "a literal name is looked up before templates". Returns
     * {@code null} when nothing matches.
     */
    public TemplateSchema resolve(String subspace) {
        return resolveIn(templates, subspace);
    }

    /** {@link #resolve} over an explicit list, so the boot check can resolve a doc example's
     *  subspace before any registry instance exists. */
    private static TemplateSchema resolveIn(List<TemplateSchema> templates, String subspace) {
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
                String address = input.get(i);
                if (!ADDRESS_SEGMENT_PATTERN.matcher(address).matches()) {
                    return false;
                }
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
        // RDR-239 S3: a bad doc block is fatal in a classpath template and dropped, with a
        // warning, in any other source (NX_TUPLE_TEMPLATE_DIR). Only the doc is forgiven.
        Set<String> docDroppable = new HashSet<>();
        List<String> sources = new ArrayList<>();

        for (SourceGroup group : groups) {
            sources.add(group.label());
            boolean droppable = !SOURCE_RESOURCES.equals(group.label());
            for (TemplateSource file : group.files()) {
                Map<String, Object> doc = MiniYaml.parse(file.descriptor(), file.yamlText());
                TemplateSchema schema = TemplateSchemaParser.parseStructure(file.descriptor(), doc);
                if (doc.containsKey("doc")) {
                    try {
                        schema = schema.withDoc(TemplateSchemaParser.parseDoc(file.descriptor(), doc.get("doc"), schema));
                    } catch (TemplateRegistryException e) {
                        if (!droppable) {
                            throw e;
                        }
                        warnDocDropped(schema.name(), file.descriptor(), e);
                    }
                }
                if (droppable) {
                    docDroppable.add(schema.name());
                }
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

        // RDR-211 Scale and Limits item 6 (bead nexus-rplay.6): a template's own
        // claim_log_ttl_seconds, when present, may only SHORTEN the engine default,
        // never lengthen it -- checked here, not in TemplateSchemaParser, because the
        // engine default is only known once this method's own caller has resolved
        // CLAIM_LOG_TTL_DAYS_ENV (TemplateSchema#claimLogTtlSeconds()'s javadoc records
        // this as a deliberate choice). The retention-vs-TTL boot check below is now
        // PER TEMPLATE too, against each template's own EFFECTIVE TTL (its override, or
        // this registry-wide default) rather than uniformly against the registry-wide
        // default alone -- so a template that shortens its own claim-log TTL is held to
        // the same "strictly more than one sweep interval past its own retention" rule
        // every other template already was.
        // RDR-211 Approach item 5 (bead nexus-rplay.17, code-review-expert finding
        // 3): lock: true and take.max_attempts are a contradiction -- lock.yaml's
        // own comment records the design intent directly ("max_attempts
        // intentionally OMITTED ... the engine treats an absent max_attempts as
        // unbounded, so a lock never dead-letters on a crashed holder's repeated
        // lease lapses"). A template shipping both would dead-letter a lock
        // holder's crashed lease after N lapses, then refuse ack on it forever
        // (the lock flag's own ack-refusal invariant at consumeClaim), stranding
        // the resource with no way back to available short of the sweep purging
        // it. Checked here, alongside the claim-log-TTL checks, rather than in
        // TemplateSchemaParser: both are cross-field checks entirely within
        // TemplateSchema's own fields, so either placement would work, and this
        // keeps every boot-time cross-field refusal in one place.
        for (TemplateSchema t : templates) {
            if (t.lock() && t.take().maxAttempts() != null) {
                throw new TemplateRegistryException(t.name(), "lock",
                        "refuses to boot: template '" + t.name() + "' declares both lock: true and "
                                + "take.max_attempts " + t.take().maxAttempts() + " -- a lock must never "
                                + "dead-letter, so the two are a contradiction");
            }
        }

        for (TemplateSchema t : templates) {
            if (t.claimLogTtlSeconds() != null && t.claimLogTtlSeconds() > claimLogTtlSeconds) {
                throw new TemplateRegistryException(t.name(), "claim_log_ttl_seconds",
                        "refuses to boot: template '" + t.name() + "' declares claim_log_ttl_seconds "
                                + t.claimLogTtlSeconds() + "s, above the engine's own claim log TTL "
                                + claimLogTtlSeconds + "s (" + CLAIM_LOG_TTL_DAYS_ENV
                                + ") -- a template may only SHORTEN it, never lengthen it");
            }
            long effectiveTtl = effectiveClaimLogTtlSeconds(t, claimLogTtlSeconds);
            long ceiling = t.retentionSeconds() + sweepIntervalSeconds;
            if (effectiveTtl <= ceiling) {
                String source = t.claimLogTtlSeconds() != null
                        ? "its own claim_log_ttl_seconds"
                        : CLAIM_LOG_TTL_DAYS_ENV + " (the engine default)";
                throw new TemplateRegistryException(t.name(), "retention_seconds",
                        "refuses to boot: template '" + t.name() + "' effective claim log TTL "
                                + effectiveTtl + "s (from " + source + ") does not exceed its own"
                                + " retention_seconds " + t.retentionSeconds()
                                + "s by strictly more than one sweep interval " + sweepIntervalSeconds + "s");
            }
        }

        templates = validateDocExamples(templates, docDroppable, firstSourceOf);

        String digest = computeDigest(templates);
        log.info("event=tuple_template_registry_loaded sources={} templates={} digest={}",
                sources, templates.size(), digest);
        return new TemplateRegistry(templates, List.copyOf(sources), digest, claimLogTtlSeconds);
    }

    /**
     * RDR-239 Technical Design item 1: every doc example must pass the checks {@code out()}
     * applies to a real post, run by the same {@link TupleShapeValidator}, and its {@code
     * subspace} must resolve, by this registry's own matching, to the template that holds it.
     * Needs the whole sorted list, so it runs after every template has parsed. A template
     * from a droppable source (S3) whose example fails loses its doc and logs a warning;
     * any other template's failure throws.
     */
    private static List<TemplateSchema> validateDocExamples(List<TemplateSchema> templates,
                                                            Set<String> docDroppable,
                                                            Map<String, String> sourceOf) {
        List<TemplateSchema> out = new ArrayList<>(templates);
        for (int i = 0; i < templates.size(); i++) {
            TemplateSchema t = templates.get(i);
            if (t.doc() == null || t.doc().example() == null) {
                continue;
            }
            TemplateRegistryException dropWith;
            try {
                dropWith = validateDocExample(t, templates, docDroppable, sourceOf);
            } catch (TemplateRegistryException e) {
                if (!docDroppable.contains(t.name())) {
                    throw e;
                }
                dropWith = e;
            }
            if (dropWith != null) {
                warnDocDropped(t.name(), sourceOf.get(t.name()), dropWith);
                out.set(i, t.withoutDoc());
            }
        }
        return List.copyOf(out);
    }

    /**
     * Checks one template's doc example. Throws on a breach; returns a non-null exception, for
     * the caller to log and act on, when the breach is a droppable (non-classpath) template
     * shadowing the example subspace of a classpath holder: the holder is shipped and fatal for
     * its own doc errors, but an operator-supplied directory must not take the engine down over
     * documentation (S3), so the holder's doc is dropped instead. Returns {@code null} when the
     * example is sound.
     */
    private static TemplateRegistryException validateDocExample(TemplateSchema t, List<TemplateSchema> templates,
                                                                Set<String> docDroppable,
                                                                Map<String, String> sourceOf) {
        TemplateDoc.Example ex = t.doc().example();
        try {
            TupleShapeValidator.checkSubspaceSize(ex.subspace());
        } catch (TooLargeException e) {
            throw docExampleBreach(t, "subspace", e.getMessage());
        }
        TemplateSchema owner = resolveIn(templates, ex.subspace());
        if (owner == null) {
            throw docExampleBreach(t, "subspace", "subspace '" + ex.subspace()
                    + "' resolves to no template; the example must resolve to '" + t.name() + "'");
        }
        if (!owner.name().equals(t.name())) {
            TemplateRegistryException breach = docExampleBreach(t, "subspace", "subspace '" + ex.subspace()
                    + "' resolves to template '" + owner.name() + "' (from " + sourceOf.get(owner.name())
                    + "), not to '" + t.name() + "', the template that declares the example");
            if (docDroppable.contains(owner.name()) && !docDroppable.contains(t.name())) {
                return breach;
            }
            throw breach;
        }
        try {
            TupleShapeValidator.validateOut(t, ex.keys(), ex.dims(), ex.body(), ex.nonce(), false);
        } catch (SchemaViolationException e) {
            throw docExampleBreach(t, shapeFieldPath(t, ex, e.field()), e.getMessage());
        } catch (TooLargeException e) {
            throw docExampleBreach(t, e.field(), e.getMessage());
        }
        return null;
    }

    /** Maps the bare field a shape refusal names back to where it sits in the example (and in
     *  the file): a key under {@code keys.}, a dimension under {@code dims.}, the nonce bare.
     *  A size refusal arrives already qualified ({@code keys.k}). */
    private static String shapeFieldPath(TemplateSchema t, TemplateDoc.Example ex, String field) {
        if (t.keys().contains(field) || ex.keys().containsKey(field)) {
            return "keys." + field;
        }
        if (t.dimensions().containsKey(field) || ex.dims().containsKey(field)) {
            return "dims." + field;
        }
        return field;
    }

    private static TemplateRegistryException docExampleBreach(TemplateSchema t, String exampleField, String detail) {
        return new TemplateRegistryException(t.name(), "doc.example." + exampleField,
                "template '" + t.name() + "' doc example fails the checks out() applies: " + detail);
    }

    private static void warnDocDropped(String template, String source, TemplateRegistryException e) {
        log.warn("event=tuple_template_doc_dropped template={} source={} field={} detail={}",
                template, source, e.field(), e.getMessage());
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

    static Map<String, Object> toCanonicalMap(TemplateSchema t) {
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
        if (t.maxBodyBytes() != null) {
            m.put("max_body_bytes", t.maxBodyBytes());
        }
        // nexus-rplay.17 (code-review-expert finding 2): lock was omitted from the
        // digest's canonical map entirely -- two registries differing ONLY in one
        // template's lock value produced the SAME digest, so a client comparing
        // digests could never detect a template flipping into (or out of) the
        // lock lifecycle. Unconditional, unlike max_live_rows/claim_log_ttl_seconds
        // below: lock is a boolean with a real false default, not an optional
        // field whose absence is itself meaningful.
        m.put("lock", t.lock());
        if (t.maxLiveRows() != null) {
            m.put("max_live_rows", t.maxLiveRows());
        }
        if (t.claimLogTtlSeconds() != null) {
            m.put("claim_log_ttl_seconds", t.claimLogTtlSeconds());
        }
        // RDR-239 S4 Q2: the doc block is part of the digest, so a doc-only edit changes it and
        // "changes whenever a template file changes" stays true. Absent doc adds no key, so a
        // template without one keeps the digest it had before the engine learned to carry doc.
        if (t.doc() != null) {
            m.put("doc", docCanonicalMap(t.doc()));
        }
        return m;
    }

    private static Map<String, Object> docCanonicalMap(TemplateDoc d) {
        Map<String, Object> m = new TreeMap<>();
        m.put("summary", d.summary());
        if (d.writers() != null) {
            m.put("writers", d.writers());
        }
        if (d.readers() != null) {
            m.put("readers", d.readers());
        }
        if (d.fields() != null) {
            Map<String, Object> fields = new TreeMap<>();
            for (Map.Entry<String, Object> e : d.fields().entrySet()) {
                fields.put(e.getKey(), canonicalDescription(e.getValue()));
            }
            m.put("fields", fields);
        }
        if (d.body() != null) {
            m.put("body", canonicalDescription(d.body()));
        }
        if (d.example() != null) {
            TemplateDoc.Example ex = d.example();
            Map<String, Object> e = new TreeMap<>();
            e.put("subspace", ex.subspace());
            if (ex.nonce() != null) {
                e.put("nonce", ex.nonce());
            }
            e.put("keys", new TreeMap<>(ex.keys()));
            e.put("dims", new TreeMap<>(ex.dims()));
            if (ex.body() != null) {
                e.put("body", ex.body());
            }
            m.put("example", e);
        }
        if (d.seeAlso() != null) {
            m.put("see_also", d.seeAlso());
        }
        return m;
    }

    /** A description (a String) or a value map (sorted, so file order never reaches the hash). */
    @SuppressWarnings("unchecked")
    private static Object canonicalDescription(Object o) {
        return o instanceof Map ? new TreeMap<>((Map<String, String>) o) : o;
    }
}
