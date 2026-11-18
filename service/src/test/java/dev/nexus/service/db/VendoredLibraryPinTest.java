// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;

/**
 * RDR-246 Phase 1 Step 4 (nexus-f3yqd.2.10): the vendored ddl_utils and dml_utils changelogs under
 * {@code src/main/resources/db/vendor/<library>/} are the bytes of the pinned upstream revision, and nothing else.
 *
 * <p>Each library has a {@code PIN.manifest} (its full revision and the git blob id of every file, as {@code git ls-tree -r}
 * reports them at that revision) and {@code pins.properties} names the revision per library. The check computes each
 * vendored file's git blob id, {@code sha1("blob <length>\0" + bytes)}, and compares it with the manifest, the manifest's
 * revision with the pin, and the file set with the manifest's. No network and no clone: a changed byte, a missing file,
 * an extra file, an empty manifest or a revision that is not the pin is red.
 * {@code scripts/vendor_dml_utils.py --update} regenerates the copies and the manifests; {@code --check} is the same
 * comparison in Python.
 */
class VendoredLibraryPinTest {

    static final Path VENDOR_ROOT = Path.of("src", "main", "resources", "db", "vendor");
    static final List<String> LIBRARIES = List.of("ddl_utils", "dml_utils");
    static final String MANIFEST = "PIN.manifest";
    static final String PINS = "pins.properties";
    static final String LICENSE = "LICENSE";

    private static final Pattern SHA = Pattern.compile("[0-9a-f]{40}");
    private static final Pattern MANIFEST_FILE_LINE = Pattern.compile("([0-9a-f]{40}) (.+)");

    record Manifest(String revision, Map<String, String> files) {}

    static String blobId(byte[] data) {
        try {
            MessageDigest sha1 = MessageDigest.getInstance("SHA-1");
            sha1.update(("blob " + data.length).getBytes(StandardCharsets.US_ASCII));
            sha1.update((byte) 0);
            return HexFormat.of().formatHex(sha1.digest(data));
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    static Manifest readManifest(Path file) throws IOException {
        String revision = "";
        Map<String, String> files = new TreeMap<>();
        for (String line : Files.readAllLines(file)) {
            if (line.isBlank() || line.startsWith("#")) continue;
            if (line.startsWith("revision ")) {
                revision = line.substring("revision ".length()).trim();
                continue;
            }
            Matcher m = MANIFEST_FILE_LINE.matcher(line);
            if (!m.matches()) throw new IllegalStateException(file + ": not a manifest line: " + line);
            files.put(m.group(2), m.group(1));
        }
        return new Manifest(revision, files);
    }

    static Map<String, String> readPins(Path file) throws IOException {
        Map<String, String> pins = new TreeMap<>();
        for (String line : Files.readAllLines(file)) {
            if (line.isBlank() || line.startsWith("#")) continue;
            int eq = line.indexOf('=');
            pins.put(line.substring(0, eq).trim(), line.substring(eq + 1).trim());
        }
        return pins;
    }

    /** Everything wrong with the vendor tree under {@code root}; empty when it matches its manifests and pins. */
    static List<String> problems(Path root) throws IOException {
        List<String> problems = new ArrayList<>();
        Map<String, String> pins = Map.of();
        if (Files.isRegularFile(root.resolve(PINS))) {
            pins = readPins(root.resolve(PINS));
        } else {
            problems.add(PINS + " is missing: there is no pin to hold the vendored files to");
        }
        for (String lib : LIBRARIES) {
            problems.addAll(libraryProblems(root.resolve(lib), lib, pins));
        }
        return problems;
    }

    private static List<String> libraryProblems(Path dir, String lib, Map<String, String> pins) throws IOException {
        Path manifestPath = dir.resolve(MANIFEST);
        if (!Files.isRegularFile(manifestPath)) return List.of(lib + ": manifest " + MANIFEST + " is missing");
        Manifest manifest = readManifest(manifestPath);
        List<String> problems = new ArrayList<>();
        if (!SHA.matcher(manifest.revision()).matches()) {
            problems.add(lib + ": manifest revision '" + manifest.revision() + "' is not a full 40-hex commit id");
        }
        if (!pins.containsKey(lib)) {
            problems.add(lib + ": no pin in " + PINS);
        } else if (!pins.get(lib).equals(manifest.revision())) {
            problems.add(lib + ": manifest revision " + manifest.revision() + " differs from the pin " + pins.get(lib));
        }
        if (manifest.files().isEmpty()) problems.add(lib + ": the manifest lists no files");
        if (!manifest.files().containsKey(LICENSE)) {
            problems.add(lib + ": the manifest does not list " + LICENSE + "; the licence notice must ship with the files");
        }
        Map<String, Path> onDisk = new TreeMap<>();
        try (Stream<Path> walk = Files.walk(dir)) {
            walk.filter(Files::isRegularFile)
                .filter(p -> !p.getFileName().toString().equals(MANIFEST))
                .forEach(p -> onDisk.put(dir.relativize(p).toString().replace('\\', '/'), p));
        }
        for (Map.Entry<String, String> e : manifest.files().entrySet()) {
            Path file = onDisk.get(e.getKey());
            if (file == null) {
                problems.add(lib + ": " + e.getKey() + " is missing from the vendor tree");
                continue;
            }
            String actual = blobId(Files.readAllBytes(file));
            if (!actual.equals(e.getValue())) {
                problems.add(lib + ": " + e.getKey() + " has blob id " + actual + ", the manifest says " + e.getValue());
            }
        }
        for (String rel : onDisk.keySet()) {
            if (!manifest.files().containsKey(rel)) problems.add(lib + ": " + rel + " is in the vendor tree but not in the manifest");
        }
        return problems;
    }

    private static void copyTree(Path from, Path to) throws IOException {
        try (Stream<Path> walk = Files.walk(from)) {
            for (Path p : (Iterable<Path>) walk::iterator) {
                Path target = to.resolve(from.relativize(p).toString());
                if (Files.isDirectory(p)) Files.createDirectories(target);
                else Files.write(target, Files.readAllBytes(p));
            }
        }
    }

    // ------------------------------------------------------------------ the checked-in tree

    @Test
    void checkedInVendorTreeMatchesItsManifestsAndPins() throws Exception {
        assertThat(problems(VENDOR_ROOT)).as("vendored files against PIN.manifest and pins.properties").isEmpty();
    }

    @Test
    void checkedInVendorTreeIsNotVacuous() throws Exception {
        Map<String, String> pins = readPins(VENDOR_ROOT.resolve(PINS));
        assertThat(pins.keySet()).as("every vendored library is pinned").containsExactlyInAnyOrderElementsOf(LIBRARIES);
        for (String lib : LIBRARIES) {
            Manifest m = readManifest(VENDOR_ROOT.resolve(lib).resolve(MANIFEST));
            assertThat(m.revision()).as(lib + " revision").matches(SHA.pattern()).isEqualTo(pins.get(lib));
            assertThat(m.files()).as(lib + " manifest files: a library this small is not the library").hasSizeGreaterThan(50);
            assertThat(m.files()).as(lib + " ships its licence notice").containsKey(LICENSE);
            assertThat(m.files()).as(lib + " master changelog").containsKey("db.changelog-master.xml");
        }
    }

    @Test
    void theVendoredChangelogsAreNotIncludedFromTheEngineMaster() throws Exception {
        // The boot-apply step runs them under their own role; an include from nexus's master would run them as nexus_admin
        // with no pg_background guard.
        String master = Files.readString(Path.of("src", "main", "resources", "db", "changelog", "db.changelog-master.xml"));
        assertThat(master).doesNotContain("db/vendor");
    }

    // ------------------------------------------------------------------ the check goes red (mutations of a copy)

    private Path copyOfVendorTree(Path tmp) throws IOException {
        Path copy = tmp.resolve("vendor");
        copyTree(VENDOR_ROOT, copy);
        assertThat(problems(copy)).as("the copy starts clean").isEmpty();
        return copy;
    }

    @Test
    void oneChangedByteIsRed(@TempDir Path tmp) throws Exception {
        Path copy = copyOfVendorTree(tmp);
        Path victim = copy.resolve("dml_utils").resolve("changes").resolve("changes.xml");
        byte[] data = Files.readAllBytes(victim);
        data[data.length / 2] ^= 1;
        Files.write(victim, data);
        assertThat(problems(copy)).anyMatch(p -> p.contains("changes/changes.xml") && p.contains("blob id"));
    }

    @Test
    void aChangedLicenceByteIsRed(@TempDir Path tmp) throws Exception {
        Path copy = copyOfVendorTree(tmp);
        Path victim = copy.resolve("ddl_utils").resolve(LICENSE);
        byte[] data = Files.readAllBytes(victim);
        data[0] ^= 1;
        Files.write(victim, data);
        assertThat(problems(copy)).anyMatch(p -> p.startsWith("ddl_utils: LICENSE") && p.contains("blob id"));
    }

    @Test
    void aDeletedFileIsRed(@TempDir Path tmp) throws Exception {
        Path copy = copyOfVendorTree(tmp);
        Files.delete(copy.resolve("dml_utils").resolve("db.changelog-master.xml"));
        assertThat(problems(copy)).anyMatch(p -> p.contains("db.changelog-master.xml") && p.contains("missing"));
    }

    @Test
    void anExtraFileIsRed(@TempDir Path tmp) throws Exception {
        Path copy = copyOfVendorTree(tmp);
        Files.writeString(copy.resolve("ddl_utils").resolve("changes").resolve("stray.sql"), "select 1;\n");
        assertThat(problems(copy)).anyMatch(p -> p.contains("stray.sql") && p.contains("not in the manifest"));
    }

    @Test
    void aManifestWithZeroFilesIsRed(@TempDir Path tmp) throws Exception {
        Path copy = copyOfVendorTree(tmp);
        Path manifest = copy.resolve("dml_utils").resolve(MANIFEST);
        List<String> kept = new ArrayList<>();
        for (String line : Files.readAllLines(manifest)) {
            if (line.startsWith("#") || line.startsWith("revision ")) kept.add(line);
        }
        Files.write(manifest, kept);
        assertThat(problems(copy)).anyMatch(p -> p.contains("lists no files"));
    }

    @Test
    void aMissingManifestIsRed(@TempDir Path tmp) throws Exception {
        Path copy = copyOfVendorTree(tmp);
        Files.delete(copy.resolve("ddl_utils").resolve(MANIFEST));
        assertThat(problems(copy)).anyMatch(p -> p.startsWith("ddl_utils") && p.contains("manifest"));
    }

    @Test
    void aManifestRevisionThatIsNotThePinIsRed(@TempDir Path tmp) throws Exception {
        Path copy = copyOfVendorTree(tmp);
        Path pins = copy.resolve(PINS);
        String dml = readPins(pins).get("dml_utils");
        Files.writeString(pins, Files.readString(pins).replace(dml, "0".repeat(40)));
        assertThat(problems(copy)).anyMatch(p -> p.startsWith("dml_utils") && p.contains("differs from the pin"));
    }

    @Test
    void anAbbreviatedRevisionIsRed(@TempDir Path tmp) throws Exception {
        Path copy = copyOfVendorTree(tmp);
        Path manifest = copy.resolve("ddl_utils").resolve(MANIFEST);
        String rev = readManifest(manifest).revision();
        Files.writeString(manifest, Files.readString(manifest).replace(rev, rev.substring(0, 9)));
        assertThat(problems(copy)).anyMatch(p -> p.contains("not a full 40-hex"));
    }

    @Test
    void anAbsentVendorTreeIsRedNotVacuouslyGreen(@TempDir Path tmp) throws Exception {
        assertThat(problems(tmp.resolve("nothing-here"))).isNotEmpty();
    }
}
