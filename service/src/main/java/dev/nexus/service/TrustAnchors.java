// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import javax.net.ssl.TrustManager;
import javax.net.ssl.TrustManagerFactory;
import javax.net.ssl.X509TrustManager;
import java.io.PrintStream;
import java.security.GeneralSecurityException;
import java.security.KeyStore;
import java.security.cert.X509Certificate;
import java.util.ArrayList;
import java.util.List;

/**
 * nexus-37n79 -- {@code nexus-service --trust-anchors}: prints the roots the engine's default
 * TrustManager accepts, then exits before the engine starts anything.
 *
 * <p>A native image embeds the build-time trust store and has no cacerts to fall back to
 * (nexus-j36wr). The build's {@code verify} step checks the executable for alias strings; this
 * mode lets {@code service/src/build/java/NativeTrustStore.java anchors} ask the built
 * executable what it actually trusts, the same way the Voyage client's TLS does: a
 * {@link TrustManagerFactory} of the default algorithm initialised with no key store.
 *
 * <p>Output, one line each, on stdout: {@code trust_anchors count=<n>}, then
 * {@code trust_anchor <subject>} per accepted issuer in RFC 2253 form. Stdout is the protocol
 * channel here; logging may share it, so the reader keys on the line prefixes.
 */
final class TrustAnchors {

    /** The command-line argument that selects this mode. */
    static final String FLAG = "--trust-anchors";

    private TrustAnchors() {
    }

    /** True when {@code args} asks for this mode and nothing else. */
    static boolean requested(String[] args) {
        return args.length == 1 && FLAG.equals(args[0]);
    }

    /** The issuers the default X509 TrustManager accepts. */
    static List<X509Certificate> accepted() throws GeneralSecurityException {
        TrustManagerFactory factory = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm());
        factory.init((KeyStore) null);
        List<X509Certificate> issuers = new ArrayList<>();
        for (TrustManager manager : factory.getTrustManagers()) {
            if (manager instanceof X509TrustManager x509) {
                issuers.addAll(List.of(x509.getAcceptedIssuers()));
            }
        }
        return issuers;
    }

    static void print(PrintStream out) throws GeneralSecurityException {
        List<X509Certificate> issuers = accepted();
        out.println("trust_anchors count=" + issuers.size());
        for (X509Certificate issuer : issuers) {
            out.println("trust_anchor " + issuer.getSubjectX500Principal().getName());
        }
        out.flush();
    }
}
