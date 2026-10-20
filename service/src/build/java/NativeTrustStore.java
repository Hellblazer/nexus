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
//
// Run as a source-file program: java NativeTrustStore.java prune|verify <path>.

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
import java.util.ArrayList;
import java.util.List;

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
            fail("usage: java NativeTrustStore.java prune <out-file> | verify <dir-or-binary>");
        }
        // nexus.truststore.source exists for the tests; the build always reads the JDK's own cacerts.
        File source = new File(System.getProperty("nexus.truststore.source",
                new File(System.getProperty("java.home"), "lib/security/cacerts").getPath()));
        KeyStore jdk = KeyStore.getInstance(source, PASSWORD);
        switch (args[0]) {
            case "prune" -> prune(jdk, Path.of(args[1]));
            case "verify" -> verify(jdk, binary(Path.of(args[1])));
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
        if (dropped == 0) {
            fail("none of " + DROP + " is in the build JDK's cacerts: check the aliases against this JDK");
        }
        // Stored the way the JDK stores cacerts: certificates and MAC unprotected, so the builder
        // (and a run-time -Djavax.net.ssl.trustStore) reads every root with no password. A store
        // written with PKCS12's defaults loads with ZERO roots when no trustStorePassword is given.
        Security.setProperty("keystore.pkcs12.certProtectionAlgorithm", "NONE");
        Security.setProperty("keystore.pkcs12.macAlgorithm", "NONE");
        Files.createDirectories(out.toAbsolutePath().getParent());
        try (OutputStream os = new FileOutputStream(out.toFile())) {
            pruned.store(os, PASSWORD);
        }
        System.out.printf("native trust store: %d roots from %s, %d of %d named roots dropped, written to %s%n",
                pruned.size(), System.getProperty("java.home"), dropped, DROP.size(), out);
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
            problems.add("checked none of " + DROP + ": this JDK names them differently");
        }
        if (!problems.isEmpty()) {
            fail(binary + " " + String.join("; ", problems));
        }
        System.out.printf("native trust store verified in %s (%d bytes): %d dropped roots absent, %d required roots present%n",
                binary, image.length, checkedDrop, KEEP.size());
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
