// SPDX-License-Identifier: AGPL-3.0-or-later
// nexus-j36wr: the trust store the native image embeds, pruned of roots we do not trust.
//
// native-image captures the build-time default trust store into the executable, and a native image
// has no cacerts file to fall back to at run time, so whatever the build JDK trusts the engine
// trusts. Its outbound TLS to api.voyageai.com carries the Voyage key, and JSSE checks no
// revocation by default.
//
//   prune <out-file>         copy ${java.home}/lib/security/cacerts without DROP into <out-file>,
//                            unprotected like cacerts itself, failing if a KEEP root is missing. service/pom.xml
//                            passes <out-file> to the builder JVM as -J-Djavax.net.ssl.trustStore.
//   verify <dir-or-binary>   search the built executable (nexus-service or nexus-service.exe in the
//                            directory) for the alias of every DROP root this JDK carries (each must
//                            be absent) and every KEEP root (each must be present, which proves the
//                            search can see the captured store at all). Aliases, not certificate
//                            bytes: the image holds every JDK root's DER through another JDK path
//                            whatever the trust store is (measured: a build whose captured store
//                            was empty still held all of them), while the captured KeyStore is the
//                            only thing that carries "<name> [jdk]" (the released v0.1.161 binary
//                            holds each alias twice).
//   anchors <dir>            nexus-37n79: run the built executable in <dir> as
//                            `nexus-service --trust-anchors`, which prints the issuers its default
//                            TrustManager accepts, and require exactly the certificates of
//                            <dir>/native-truststore/cacerts (the store prune wrote): the same count,
//                            the same SHA-256 fingerprints and subjects, no subject of the distrusted
//                            hierarchy, every KEEP root present. This is what the engine's TLS trusts, not alias strings.
//
// Run as a source-file program: java NativeTrustStore.java prune|verify|anchors <path>.

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.GeneralSecurityException;
import java.security.KeyStore;
import java.security.Security;
import java.security.cert.Certificate;
import java.security.cert.X509Certificate;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.TreeSet;
import java.util.concurrent.TimeUnit;

public class NativeTrustStore {

    /**
     * Roots left out of the image, by JDK cacerts alias. AAA Certificate Services cross-signed the
     * eight eMudhra issuing CAs revoked for compromise on 2026-10-18 (Sectigo revoked those copies
     * on 2026-10-19); the emSign roots are their own hierarchy. Reported by conexus, 2026-10-20.
     */
    static final List<String> DROP = List.of(
            "comodoaaaca [jdk]", "emsignrootcag1 [jdk]", "emsignrootcag2 [jdk]", "emsigneccrootcag3 [jdk]");

    /**
     * Roots that must survive: api.voyageai.com serves leaf, WR3, GTS Root R1 cross-signed by
     * GlobalSign Root CA, so either anchor completes its chain.
     */
    static final List<String> KEEP = List.of("globalsignca [jdk]", "gtsrootcar1 [jdk]");

    /**
     * A root of the compromised hierarchy under a name DROP does not list (another JDK's alias, or
     * the emSign G3 roots the eMudhra intermediates actually chain to, absent from JDK 25.0.3) fails
     * prune instead of being kept silently.
     */
    static final java.util.regex.Pattern DISTRUSTED_SUBJECT =
            java.util.regex.Pattern.compile("emSign|eMudhra|AAA Certificate Services", java.util.regex.Pattern.CASE_INSENSITIVE);

    static final char[] PASSWORD = "changeit".toCharArray();

    public static void main(String[] args) throws Exception {
        if (args.length != 2) {
            fail("usage: java NativeTrustStore.java prune <out-file> | verify <dir-or-binary> | anchors <dir>");
        }
        // nexus.truststore.source exists for the tests; the build always reads the JDK's own cacerts.
        File source = new File(System.getProperty("nexus.truststore.source",
                new File(System.getProperty("java.home"), "lib/security/cacerts").getPath()));
        KeyStore jdk = KeyStore.getInstance(source, PASSWORD);
        switch (args[0]) {
            case "prune" -> prune(jdk, Path.of(args[1]));
            case "verify" -> verify(jdk, binary(Path.of(args[1])));
            case "anchors" -> anchors(jdk, Path.of(args[1]));
            default -> fail("unknown mode " + args[0]);
        }
    }

    static void prune(KeyStore jdk, Path out) throws GeneralSecurityException, IOException {
        for (String keep : KEEP) {
            if (!jdk.containsAlias(keep)) {
                fail("the build JDK's cacerts has no " + keep + "; Voyage's chain would not verify");
            }
        }
        KeyStore pruned = KeyStore.getInstance("PKCS12");
        pruned.load(null, PASSWORD);
        int dropped = 0;
        for (String alias : java.util.Collections.list(jdk.aliases())) {
            if (DROP.contains(alias)) {
                dropped++;
                continue;
            }
            pruned.setCertificateEntry(alias, jdk.getCertificate(alias));
        }
        for (String alias : java.util.Collections.list(pruned.aliases())) {
            String subject = ((java.security.cert.X509Certificate) pruned.getCertificate(alias))
                    .getSubjectX500Principal().getName();
            if (DISTRUSTED_SUBJECT.matcher(subject).find()) {
                fail("kept root " + alias + " (" + subject + ") is in the distrusted hierarchy: add it to DROP");
            }
        }
        // A JDK whose cacerts was already pruned of these roots (the CVE-2026-91403 pool sweep, and
        // patched GraalVM builds) carries none of them. That is the end state this step exists to
        // reach, so it passes; the subject check above is what fails a build that keeps a root of
        // the distrusted hierarchy under a name DROP does not list.
        String absent = dropped == 0 ? " (already absent from this JDK)" : "";
        // Stored the way the JDK stores cacerts: certificates and MAC unprotected, so the builder
        // (and a run-time -Djavax.net.ssl.trustStore) reads every root with no password. A store
        // written with PKCS12's defaults loads with ZERO roots when no trustStorePassword is given.
        Security.setProperty("keystore.pkcs12.certProtectionAlgorithm", "NONE");
        Security.setProperty("keystore.pkcs12.macAlgorithm", "NONE");
        Files.createDirectories(out.toAbsolutePath().getParent());
        try (OutputStream os = new FileOutputStream(out.toFile())) {
            pruned.store(os, PASSWORD);
        }
        System.out.printf("native trust store: %d roots from %s, %d of %d named roots dropped%s, written to %s%n",
                pruned.size(), System.getProperty("java.home"), dropped, DROP.size(), absent, out);
    }

    static void verify(KeyStore jdk, Path binary) throws GeneralSecurityException, IOException {
        byte[] image = Files.readAllBytes(binary);
        List<String> problems = new ArrayList<>();
        int checkedDrop = 0;
        for (String alias : DROP) {
            Certificate cert = jdk.getCertificate(alias);
            if (cert == null) {
                continue; // this JDK no longer ships it, so nothing can have embedded it
            }
            checkedDrop++;
            if (indexOf(image, alias.getBytes(StandardCharsets.US_ASCII)) >= 0) {
                problems.add("embeds dropped root " + alias);
            }
        }
        for (String alias : KEEP) {
            if (indexOf(image, alias.getBytes(StandardCharsets.US_ASCII)) < 0) {
                problems.add("does not embed required root " + alias);
            }
        }
        if (checkedDrop == 0) {
            // This JDK no longer ships any of them (already pruned). Nothing named can be embedded;
            // what could be is a root of the same hierarchy under another alias, so check by subject.
            for (String alias : java.util.Collections.list(jdk.aliases())) {
                String subject = ((X509Certificate) jdk.getCertificate(alias)).getSubjectX500Principal().getName();
                if (DISTRUSTED_SUBJECT.matcher(subject).find()) {
                    problems.add("this JDK carries distrusted root " + alias + " (" + subject + ") under a name DROP does not list");
                }
            }
        }
        if (!problems.isEmpty()) {
            fail(binary + " " + String.join("; ", problems));
        }
        System.out.printf("native trust store verified in %s (%d bytes): %d dropped roots absent, %d required roots present%n",
                binary, image.length, checkedDrop, KEEP.size());
    }

    static void anchors(KeyStore jdk, Path dir) throws GeneralSecurityException, IOException, InterruptedException {
        Path storeFile = dir.resolve("native-truststore").resolve("cacerts");
        KeyStore pruned = KeyStore.getInstance(storeFile.toFile(), PASSWORD);
        Set<Certificate> expected = new HashSet<>();
        for (String alias : java.util.Collections.list(pruned.aliases())) {
            expected.add(pruned.getCertificate(alias));
        }
        Set<String> expectedSubjects = new TreeSet<>();
        Set<String> expectedFingerprints = new TreeSet<>();
        for (Certificate cert : expected) {
            expectedSubjects.add(((X509Certificate) cert).getSubjectX500Principal().getName());
            expectedFingerprints.add(sha256(cert));
        }

        // nexus.truststore.probe exists for the tests: a Java source file that stands in for the
        // executable. The build always runs the executable it just made.
        String probe = System.getProperty("nexus.truststore.probe");
        List<String> command = probe != null
                ? List.of(new File(System.getProperty("java.home"), "bin/java").getPath(), probe, "--trust-anchors")
                : List.of(binary(dir).toString(), "--trust-anchors");
        String text;
        Path captured = Files.createTempFile("nexus-trust-anchors", ".txt");
        try {
            Process process = new ProcessBuilder(command).redirectErrorStream(true)
                    .redirectOutput(captured.toFile()).start();
            boolean exited = process.waitFor(120, TimeUnit.SECONDS);
            if (!exited) {
                process.destroyForcibly().waitFor(10, TimeUnit.SECONDS);
            }
            text = new String(Files.readAllBytes(captured), StandardCharsets.UTF_8);
            if (!exited) {
                fail(command + " did not exit within 120 s; output so far: " + text);
            }
            if (process.exitValue() != 0) {
                fail(command + " exited " + process.exitValue() + ": " + text);
            }
        } finally {
            Files.deleteIfExists(captured);
        }
        int reported = -1;
        List<String> subjects = new ArrayList<>();
        Set<String> fingerprints = new TreeSet<>();
        for (String line : text.split("\\R")) {
            if (line.startsWith("trust_anchors count=")) {
                reported = Integer.parseInt(line.substring("trust_anchors count=".length()).trim());
            } else if (line.startsWith("trust_anchor sha256=")) {
                String rest = line.substring("trust_anchor sha256=".length());
                int space = rest.indexOf(' ');
                fingerprints.add(space < 0 ? rest : rest.substring(0, space));
                subjects.add(space < 0 ? "" : rest.substring(space + 1));
            }
        }

        List<String> problems = new ArrayList<>();
        if (reported < 0) {
            problems.add("printed no trust_anchors line: " + text);
        } else if (reported != subjects.size()) {
            problems.add("reported " + reported + " anchors but listed " + subjects.size());
        }
        if (subjects.size() != expected.size()) {
            problems.add("trusts " + subjects.size() + " roots, the pruned store holds " + expected.size());
        }
        for (String subject : subjects) {
            if (DISTRUSTED_SUBJECT.matcher(subject).find()) {
                problems.add("trusts " + subject);
            }
        }
        for (String alias : KEEP) {
            Certificate keep = jdk.getCertificate(alias);
            if (!fingerprints.contains(sha256(keep))) {
                problems.add("does not trust required root " + alias + " ("
                        + ((X509Certificate) keep).getSubjectX500Principal().getName() + ")");
            }
        }
        if (!new TreeSet<>(subjects).equals(expectedSubjects)) {
            Set<String> extra = new TreeSet<>(subjects);
            extra.removeAll(expectedSubjects);
            Set<String> missing = new TreeSet<>(expectedSubjects);
            missing.removeAll(subjects);
            problems.add("subjects differ from the pruned store: extra " + extra + ", missing " + missing);
        }
        if (!fingerprints.equals(expectedFingerprints)) {
            Set<String> extra = new TreeSet<>(fingerprints);
            extra.removeAll(expectedFingerprints);
            Set<String> missing = new TreeSet<>(expectedFingerprints);
            missing.removeAll(fingerprints);
            problems.add("certificates differ from the pruned store: extra sha256 " + extra + ", missing sha256 " + missing);
        }
        if (!problems.isEmpty()) {
            fail(command.get(command.size() - 2) + " " + String.join("; ", problems));
        }
        System.out.printf("native trust anchors verified: the executable trusts %d roots, exactly the certificates of %s, none distrusted, %d required present%n",
                subjects.size(), storeFile, KEEP.size());
    }

    static String sha256(Certificate cert) throws GeneralSecurityException {
        return java.util.HexFormat.of().formatHex(
                java.security.MessageDigest.getInstance("SHA-256").digest(cert.getEncoded()));
    }

    static Path binary(Path path) {
        if (Files.isRegularFile(path)) {
            return path;
        }
        for (String name : List.of("nexus-service", "nexus-service.exe")) {
            Path candidate = path.resolve(name);
            if (Files.isRegularFile(candidate)) {
                return candidate;
            }
        }
        fail("no nexus-service executable in " + path);
        return null;
    }

    static int indexOf(byte[] haystack, byte[] needle) {
        outer:
        for (int i = 0; i <= haystack.length - needle.length; i++) {
            if (haystack[i] != needle[0]) {
                continue;
            }
            for (int j = 1; j < needle.length; j++) {
                if (haystack[i + j] != needle[j]) {
                    continue outer;
                }
            }
            return i;
        }
        return -1;
    }

    static void fail(String message) {
        System.err.println("NativeTrustStore: " + message);
        System.exit(1);
    }
}
