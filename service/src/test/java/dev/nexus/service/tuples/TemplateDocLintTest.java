// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.tuples;

import dev.nexus.service.NexusService;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeSet;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * RDR-239 Phase 3, bead nexus-mdbds.2.19 (Technical Design item 5; decisions S8 = a Java test
 * over {@link TemplateRegistry#loadAtBoot}, S7 reserved example names, S1 Q2 the
 * {@code Unverified:} prefix, S1 Q4 cited paths and RDR ids).
 *
 * <p>Reads the doc blocks through the engine's own parser (the registry the engine boots), never
 * a second YAML reader. One test method per check, so a red names the check that caught it. Each
 * check fails loud when it examined nothing, and prints what it examined and exempted.
 *
 * <p>Placeholder rules, as built:
 * <ul>
 *   <li>A placeholder slot of the template name ({@code <topic>}) takes an example segment that
 *       starts with {@code example}. Only {@code mailbox/<address>} may instead take a reserved
 *       UUID in its slot (the mailbox address is a session id and cannot start with a word).
 *       Literal segments ({@code aspects}, the {@code ci} of {@code board/ci/<topic>}) are exempt.</li>
 *   <li>Every UUID-shaped run in the example (subspace, nonce, keys, dims, body) must be
 *       {@code 00000000-0000-0000-0000-00000000000[0-9]}.</li>
 *   <li>Every 40-hex run (a git sha) and every 64-hex run (a sha-256, such as the aspects
 *       {@code content_hash}) must be all zeros. The RDR rule names only 40-hex; 64 is added
 *       because the aspects example carries a 64-hex hash and a real one would identify a
 *       real document.</li>
 * </ul>
 */
class TemplateDocLintTest {

    private static final long SWEEP_INTERVAL_SECONDS = NexusService.SWEEP_INTERVAL_HOURS * 3600L;

    /** The templates whose writers and readers are intent, not evidence (S1 Q2). Hand-kept: when
     *  a template gains a verified writer and reader, drop it here in the same change that drops
     *  the prefix. */
    private static final List<String> UNVERIFIED_TEMPLATES = List.of("board/<topic>", "queue/<name>");
    private static final String UNVERIFIED_PREFIX = "Unverified: ";
    /** The one template whose placeholder slot may hold a reserved UUID instead of an {@code example...} segment. */
    private static final String RESERVED_UUID_SLOT_TEMPLATE = "mailbox/<address>";

    private static final Pattern PLACEHOLDER_SEGMENT = Pattern.compile("<[^>]+>");
    private static final Pattern UUID_SHAPED = Pattern.compile(
            "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}");
    private static final Pattern RESERVED_UUID = Pattern.compile(
            "00000000-0000-0000-0000-00000000000[0-9]");
    private static final Pattern SHA_HEX = Pattern.compile(
            "(?<![0-9a-fA-F])(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})(?![0-9a-fA-F])");
    /** A repository path: at least one slash and a file extension, not the tail of a URL. A
     *  module or symbol name (nexus.health) has no slash; a subspace (board/&lt;topic&gt;) has no
     *  extension; neither is checked. */
    private static final Pattern REPO_PATH = Pattern.compile(
            "(?<![\\w/.:-])([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+\\.(?:sh|md|py|java|yaml|yml|json|xml|toml|txt))(?![\\w/-])");
    private static final Pattern RDR_ID = Pattern.compile("RDR-(\\d{3,4})");
    /** A link to a file of this repository on GitHub. A doc block reaches a user with no checkout
     *  ({@code tuple_registry(doc=true)}), so it links instead of naming a repo path; the link's
     *  target is a cited path all the same and must exist (nexus-z7ddd). */
    private static final Pattern GITHUB_TARGET = Pattern.compile(
            "https://github\\.com/Hellblazer/nexus/(?:blob|tree)/main/([\\w./-]+)");

    private static TemplateRegistry registry;
    private static Path repoRoot;

    @BeforeAll
    static void load() {
        registry = TemplateRegistry.loadAtBoot(null, null, SWEEP_INTERVAL_SECONDS);
        Path cwd = Path.of("").toAbsolutePath();
        // Maven runs a module's tests with the module directory as cwd; the repo root is its parent.
        repoRoot = "service".equals(String.valueOf(cwd.getFileName())) ? cwd.getParent() : cwd;
        assertTrue(Files.isDirectory(repoRoot.resolve("docs/rdr")),
                "docs/rdr not found under the repository root " + repoRoot + " (cwd " + cwd
                        + "): the lint cannot check cited RDR ids, and does not skip");
    }

    // ── 1: summary and example on every loaded template ─────────────────

    @Test
    void everyLoadedTemplateCarriesDocSummaryAndExample() {
        List<TemplateSchema> templates = registry.templates();
        assertTrue(templates.size() > 0, "the registry loaded no templates: nothing examined");
        List<String> problems = new ArrayList<>();
        for (TemplateSchema t : templates) {
            TemplateDoc doc = t.doc();
            if (doc == null) {
                problems.add(t.name() + ": no doc block");
                continue;
            }
            if (doc.summary() == null || doc.summary().isBlank()) {
                problems.add(t.name() + ": doc.summary is missing or blank");
            }
            if (doc.example() == null) {
                problems.add(t.name() + ": doc.example is missing");
            }
        }
        report("check 1 (summary + example)", "templates examined=" + templates.size());
        assertTrue(problems.isEmpty(), "doc blocks incomplete:\n" + String.join("\n", problems));
    }

    // ── 2: templates directory == RESOURCE_TEMPLATE_PATHS ───────────────

    @Test
    void templatesDirectoryAndResourceListNameTheSameFiles() throws IOException {
        Path dir = repoRoot.resolve("service/src/main/resources/tuples/templates");
        assertTrue(Files.isDirectory(dir), "templates directory not found: " + dir);
        TreeSet<String> onDisk = new TreeSet<>();
        try (DirectoryStream<Path> ds = Files.newDirectoryStream(dir)) {
            for (Path p : ds) {
                String n = p.getFileName().toString();
                if (n.endsWith(".yaml") || n.endsWith(".yml")) {
                    onDisk.add("/tuples/templates/" + n);
                }
            }
        }
        assertTrue(onDisk.size() > 0, "no template files found in " + dir + ": nothing examined");
        TreeSet<String> listed = new TreeSet<>(TemplateRegistry.RESOURCE_TEMPLATE_PATHS);
        List<String> problems = new ArrayList<>();
        for (String f : onDisk) {
            if (!listed.contains(f)) {
                problems.add(f + " is in the templates directory but not in RESOURCE_TEMPLATE_PATHS, so it never loads");
            }
        }
        for (String f : listed) {
            if (!onDisk.contains(f)) {
                problems.add(f + " is in RESOURCE_TEMPLATE_PATHS but has no file in the templates directory");
            }
            if (TemplateRegistry.class.getResource(f) == null) {
                problems.add(f + " is in RESOURCE_TEMPLATE_PATHS but is not on the classpath");
            }
        }
        if (registry.templates().size() != TemplateRegistry.RESOURCE_TEMPLATE_PATHS.size()) {
            problems.add("registry loaded " + registry.templates().size() + " templates from "
                    + TemplateRegistry.RESOURCE_TEMPLATE_PATHS.size() + " listed paths");
        }
        report("check 2 (directory vs list)",
                "files on disk=" + onDisk.size() + " listed=" + listed.size());
        assertTrue(problems.isEmpty(), "templates directory and RESOURCE_TEMPLATE_PATHS differ:\n"
                + String.join("\n", problems));
    }

    // ── 3: reserved example names (S7) ──────────────────────────────────
    //
    // What checks 3 and 4 do NOT cover (nexus-mdbds.2.17 review L6; the Python mirror,
    // tests/test_tuple_template_doc_paths_lint.py, carries the same list):
    //   - paths whose extension is outside the REPO_PATH list (sh, md, py, java, yaml, yml,
    //     json, xml, toml, txt), extensionless paths (scripts/lab-run), directories, and
    //     root-level files (CHANGELOG.md has no slash);
    //   - prose fields other than writers, readers and see_also (summary, fields, body);
    //   - cross-repo paths (a path in another repository does not exist under this root);
    //   - hex runs of any length but 40 and 64.

    @Test
    void examplesUseOnlyReservedPlaceholderNames() {
        List<String> problems = new ArrayList<>();
        int examples = 0, segmentsChecked = 0, literalSegmentsExempt = 0, uuidsChecked = 0, shasChecked = 0;
        for (TemplateSchema t : registry.templates()) {
            TemplateDoc.Example ex = t.doc() == null ? null : t.doc().example();
            if (ex == null) {
                continue; // check 1 reports the absence
            }
            examples++;
            String[] nameSegs = t.name().split("/");
            String[] exSegs = String.valueOf(ex.subspace()).split("/");
            if (nameSegs.length != exSegs.length) {
                problems.add(t.name() + ": example.subspace '" + ex.subspace() + "' has "
                        + exSegs.length + " segments, the template has " + nameSegs.length);
            } else {
                for (int i = 0; i < nameSegs.length; i++) {
                    if (!PLACEHOLDER_SEGMENT.matcher(nameSegs[i]).matches()) {
                        literalSegmentsExempt++;
                        if (!nameSegs[i].equals(exSegs[i])) {
                            problems.add(t.name() + ": literal segment '" + nameSegs[i]
                                    + "' differs in example.subspace '" + ex.subspace() + "'");
                        }
                        continue;
                    }
                    segmentsChecked++;
                    boolean reservedUuidOk = t.name().equals(RESERVED_UUID_SLOT_TEMPLATE)
                            && RESERVED_UUID.matcher(exSegs[i]).matches();
                    if (!exSegs[i].startsWith("example") && !reservedUuidOk) {
                        problems.add(t.name() + ": example.subspace segment '" + exSegs[i]
                                + "' fills a placeholder; it must start with 'example'"
                                + (RESERVED_UUID_SLOT_TEMPLATE.equals(t.name()) ? " or be a reserved UUID" : ""));
                    }
                }
            }
            Map<String, String> values = new java.util.LinkedHashMap<>();
            values.put("subspace", ex.subspace());
            values.put("nonce", ex.nonce());
            ex.keys().forEach((k, v) -> values.put("keys." + k, v));
            ex.dims().forEach((k, v) -> values.put("dims." + k, v));
            values.put("body", ex.body());
            for (Map.Entry<String, String> e : values.entrySet()) {
                String v = e.getValue();
                if (v == null) {
                    continue;
                }
                Matcher u = UUID_SHAPED.matcher(v);
                while (u.find()) {
                    uuidsChecked++;
                    if (!RESERVED_UUID.matcher(u.group()).matches()) {
                        problems.add(t.name() + ": example " + e.getKey() + " holds the UUID " + u.group()
                                + "; only 00000000-0000-0000-0000-00000000000[0-9] is reserved");
                    }
                }
                Matcher h = SHA_HEX.matcher(v);
                while (h.find()) {
                    shasChecked++;
                    if (!h.group().chars().allMatch(c -> c == '0')) {
                        problems.add(t.name() + ": example " + e.getKey() + " holds the " + h.group().length()
                                + "-hex value " + h.group() + "; it must be all zeros");
                    }
                }
            }
        }
        assertTrue(examples > 0, "no example examined: the placeholder check ran over nothing");
        assertTrue(uuidsChecked > 0 && shasChecked > 0,
                "the tree carries UUID and sha examples; a run that saw none checked nothing");
        report("check 3 (reserved names)", "examples=" + examples + " placeholder segments=" + segmentsChecked
                + " literal segments exempt=" + literalSegmentsExempt + " UUIDs=" + uuidsChecked
                + " sha-length hex values=" + shasChecked);
        assertTrue(problems.isEmpty(), "example placeholder rule broken:\n" + String.join("\n", problems));
    }

    // ── 4: cited repository paths and RDR ids exist (S1 Q4) ─────────────

    @Test
    void citedRepositoryPathsAndRdrIdsExist() throws IOException {
        List<String> problems = new ArrayList<>();
        int paths = 0, rdrs = 0;
        for (TemplateSchema t : registry.templates()) {
            TemplateDoc doc = t.doc();
            if (doc == null) {
                continue;
            }
            Map<String, List<String>> prose = new java.util.LinkedHashMap<>();
            prose.put("writers", doc.writers());
            prose.put("readers", doc.readers());
            prose.put("see_also", doc.seeAlso());
            for (Map.Entry<String, List<String>> e : prose.entrySet()) {
                if (e.getValue() == null) {
                    continue;
                }
                for (String line : e.getValue()) {
                    Matcher p = REPO_PATH.matcher(line);
                    while (p.find()) {
                        paths++;
                        if (!Files.exists(repoRoot.resolve(p.group(1)))) {
                            problems.add(t.name() + ": " + e.getKey() + " cites " + p.group(1)
                                    + ", which does not exist under " + repoRoot);
                        }
                    }
                    Matcher g = GITHUB_TARGET.matcher(line);
                    while (g.find()) {
                        paths++;
                        if (!Files.exists(repoRoot.resolve(g.group(1)))) {
                            problems.add(t.name() + ": " + e.getKey() + " links " + g.group(1)
                                    + ", which does not exist under " + repoRoot);
                        }
                    }
                    Matcher r = RDR_ID.matcher(line);
                    while (r.find()) {
                        rdrs++;
                        if (!rdrFileExists(r.group(1))) {
                            problems.add(t.name() + ": " + e.getKey() + " cites RDR-" + r.group(1)
                                    + ", which has no docs/rdr/rdr-" + r.group(1) + "-*.md");
                        }
                    }
                }
            }
        }
        assertTrue(paths > 0 && rdrs > 0,
                "the tree cites repository paths and RDR ids; a run that saw none checked nothing");
        report("check 4 (cited paths + RDR ids)", "paths checked=" + paths + " RDR ids checked=" + rdrs);
        assertTrue(problems.isEmpty(), "doc blocks cite things that do not exist:\n" + String.join("\n", problems));
    }

    private static boolean rdrFileExists(String id) throws IOException {
        try (DirectoryStream<Path> ds = Files.newDirectoryStream(repoRoot.resolve("docs/rdr"),
                "rdr-" + id + "-*.md")) {
            return ds.iterator().hasNext();
        }
    }

    // ── 5: Unverified: prefix on board and queue (S1 Q2) ────────────────

    @Test
    void boardAndQueueWritersAndReadersAreMarkedUnverified() {
        List<String> problems = new ArrayList<>();
        int lines = 0;
        for (String name : UNVERIFIED_TEMPLATES) {
            TemplateSchema t = registry.templates().stream()
                    .filter(s -> s.name().equals(name)).findFirst().orElse(null);
            assertNotNull(t, "template " + name + " is not loaded; the Unverified check ran over nothing");
            TemplateDoc doc = t.doc();
            assertNotNull(doc, name + " has no doc block");
            for (Map.Entry<String, List<String>> e : Map.of("writers", orEmpty(doc.writers()),
                    "readers", orEmpty(doc.readers())).entrySet()) {
                for (String line : e.getValue()) {
                    lines++;
                    if (!line.startsWith(UNVERIFIED_PREFIX)) {
                        problems.add(name + ": a " + e.getKey() + " entry lacks the '" + UNVERIFIED_PREFIX
                                + "' prefix: " + line);
                    }
                }
            }
        }
        assertTrue(lines > 0, "no writers or readers line examined on " + UNVERIFIED_TEMPLATES);
        report("check 5 (Unverified prefix)", "templates=" + UNVERIFIED_TEMPLATES.size() + " lines=" + lines);
        assertEquals(List.of(), problems, "unverified writers/readers must say so");
    }

    private static List<String> orEmpty(List<String> l) {
        return l == null ? List.of() : l;
    }

    private static void report(String check, String counts) {
        System.out.println("TemplateDocLintTest " + check + ": " + counts);
    }
}
