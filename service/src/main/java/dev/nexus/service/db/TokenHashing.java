package dev.nexus.service.db;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.HexFormat;

/**
 * RDR-152 bead nexus-gmiaf.32.2 — canonical token-hash function.
 *
 * <p>Raw bearer/session tokens are NEVER stored or compared in the clear. Issuance
 * (Phase E / Phase C) stores {@code sha256Hex(rawToken)} in
 * {@code service_tokens.token_hash} / {@code session_tokens.session_token_hash};
 * {@link AuthFilter} hashes the presented token the same way and resolves it by an
 * indexed PK equality. Centralising the hash here keeps the issuer and the verifier
 * in lock-step (a drift between them would silently 401 every client).
 *
 * <p>SHA-256 lower-case hex (64 chars). The DB column is TEXT; the 32-char design
 * note in the bead refers to the legacy derived-token width, not this hash.
 */
public final class TokenHashing {

    private TokenHashing() {
    }

    /**
     * @param rawToken the presented or issued raw token (must not be null)
     * @return lower-case hex SHA-256 of the UTF-8 token bytes
     */
    public static String sha256Hex(String rawToken) {
        if (rawToken == null) {
            throw new IllegalArgumentException("rawToken must not be null");
        }
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            byte[] digest = md.digest(rawToken.getBytes(StandardCharsets.UTF_8));
            return HexFormat.of().formatHex(digest);
        } catch (NoSuchAlgorithmException e) {
            // SHA-256 is mandated by the JLS platform spec; absence is unrecoverable.
            throw new IllegalStateException("SHA-256 unavailable", e);
        }
    }
}
