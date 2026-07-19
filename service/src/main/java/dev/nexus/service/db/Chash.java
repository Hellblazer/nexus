/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import com.fasterxml.jackson.annotation.JsonCreator;
import com.fasterxml.jackson.annotation.JsonValue;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.Arrays;
import java.util.HexFormat;

/**
 * Content address of a chunk — parse-don't-validate newtype (nexus-z4skl,
 * inverted to the full digest by RDR-180 / nexus-jxizy.7).
 *
 * <p>A chash IS the 32-byte SHA-256 digest of the chunk text — the FULL
 * digest, never truncated. Binary is the storage form ({@code bytea},
 * {@code CHECK (octet_length(chash) = 32)}); 64 lowercase hex chars are the
 * interchange form (JSON wire values, {@code chash:<hex>} citations). This
 * type is the single encode/decode seam on the engine side: parse at the
 * boundary, get a uniform 400 with the offending length BEFORE any
 * transaction; the DB CHECK demotes to belt-and-suspenders.
 *
 * <p>HISTORY (why the polarity here is an INVERSION, not a widen): until
 * RDR-180 the stored chash was {@code sha256(text)[:32]} — 32 hex chars =
 * 128 bits = HALF the digest — while the citation grammar advertised the
 * full 64. This type was byte[16]-backed and its hints steered callers to
 * truncate deliberately. Post-flip that advice is actively wrong: 64-hex is
 * the canonical accept, and a bare 32-hex value is a LEGACY REFERENCE that
 * must be resolved through the persisted {@code nexus.chash_alias} map
 * (RDR-180 Item6) — never silently truncated, padded, or guessed. The
 * type itself never consults the DB, so {@link #fromHex} REJECTS 32-hex
 * with a message that names the alias-resolution path; read seams that
 * accept legacy references do the alias lookup first and construct the
 * Chash from the resolved value.
 *
 * <p>The former two-tier boundary ({@code requireLength32}'s length-only
 * tolerance for Chroma-era non-hex 32-char ids) is COLLAPSED: post-rekey
 * every surviving row id is a real 32-byte digest, so the type constructor
 * is the one strict tier ({@code PgVectorServingContractTest} proves the
 * ETL-era tolerance is retired).
 */
public final class Chash {

    /** Canonical interchange width: 64 lowercase hex chars (256 bits). */
    public static final int HEX_LENGTH = 64;
    /** Internal/storage width: 32 bytes — the full SHA-256 digest. */
    public static final int BYTE_LENGTH = 32;

    private static final HexFormat HEX = HexFormat.of();  // lowercase by default

    private final byte[] bytes;

    private Chash(byte[] bytes) {
        this.bytes = bytes;
    }

    /**
     * Parse the canonical 64-lowercase-hex form. The single validation
     * chokepoint — error messages carry the ACTUAL length, and the classic
     * legacy mistake (a 32-hex pre-RDR-180 chunk id) is self-diagnosing:
     * it names the chash_alias resolution path instead of inviting
     * truncation or padding.
     *
     * @throws IllegalArgumentException on null, wrong length, uppercase or
     *         non-hex input.
     */
    @JsonCreator
    public static Chash fromHex(String hex) {
        if (hex == null) {
            throw new IllegalArgumentException(
                "invalid chash: expected " + HEX_LENGTH
                + " lowercase hex chars (the full sha256 content address), got null");
        }
        if (hex.length() != HEX_LENGTH) {
            String hint = hex.length() == 32
                ? " — a legacy 32-hex (pre-RDR-180 half-digest) chunk id? for a"
                    + " READ, resolve it through the chash_alias map first; for a"
                    + " WRITE, the sending client predates RDR-180 — upgrade it"
                    + " (never truncate or pad)"
                : "";
            throw new IllegalArgumentException(
                "invalid chash: expected " + HEX_LENGTH
                + " lowercase hex chars (the full sha256 content address), got "
                + hex.length() + " chars" + hint);
        }
        // LOAD-BEARING, not redundant with parseHex below: HexFormat.parseHex
        // is case-INsensitive by contract regardless of the formatter's case,
        // so this loop is what actually enforces LOWERCASE-only (review note).
        for (int i = 0; i < hex.length(); i++) {
            char c = hex.charAt(i);
            boolean ok = (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
            if (!ok) {
                throw new IllegalArgumentException(
                    "invalid chash: expected " + HEX_LENGTH
                    + " LOWERCASE hex chars, got '" + c + "' at index " + i);
            }
        }
        return new Chash(HEX.parseHex(hex));
    }

    /**
     * The identity constructor for a full sha256 hex (RDR-180: it no longer
     * truncates — the full digest IS the chash). Retained as the named
     * migration point so pre-flip call sites keep compiling and now do the
     * right thing; lowercases for convenience since digest renderers vary.
     */
    public static Chash fromSha256Hex(String fullSha256Hex) {
        if (fullSha256Hex == null || fullSha256Hex.length() != HEX_LENGTH) {
            throw new IllegalArgumentException(
                "fromSha256Hex expects the FULL " + HEX_LENGTH + "-char sha256 hex, got "
                + (fullSha256Hex == null ? "null" : fullSha256Hex.length() + " chars"));
        }
        return fromHex(fullSha256Hex.toLowerCase());
    }

    /** Wrap a raw 32-byte SHA-256 digest (defensive copy). */
    public static Chash fromSha256Bytes(byte[] digest) {
        if (digest == null || digest.length != BYTE_LENGTH) {
            throw new IllegalArgumentException(
                "fromSha256Bytes expects exactly " + BYTE_LENGTH + " bytes, got "
                + (digest == null ? "null" : digest.length + " bytes"));
        }
        return new Chash(Arrays.copyOf(digest, BYTE_LENGTH));
    }

    /** Compute the canonical chash of chunk text (FULL sha256 over UTF-8). */
    public static Chash ofText(String chunkText) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            return new Chash(md.digest(chunkText.getBytes(StandardCharsets.UTF_8)));
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 unavailable", e);  // JVM-guaranteed algorithm
        }
    }

    /**
     * Boundary convenience (nexus-e0hd2 lineage): parse-don't-validate
     * *value* and return its canonical hex, prefixing *label* into the
     * rejection message so a 400 names the offending field/position. The
     * Chash type stays the sole enforcement point; handlers call this
     * instead of hand-rolling. (The former {@code requireLength32}
     * permissive tier is gone — RDR-180 Item3's one-strict-tier collapse.)
     */
    public static String requireCanonical(String value, String label) {
        try {
            return fromHex(value).toHex();
        } catch (IllegalArgumentException e) {
            throw new IllegalArgumentException(label + ": " + e.getMessage());
        }
    }

    /** The raw 32-byte storage form (defensive copy) — the jOOQ bind value. */
    public byte[] toBytes() {
        return Arrays.copyOf(bytes, bytes.length);
    }

    /** Canonical 64-char lowercase hex rendering (the wire form). */
    @JsonValue
    public String toHex() {
        return HEX.formatHex(bytes);
    }

    @Override
    public String toString() {
        return toHex();
    }

    @Override
    public boolean equals(Object o) {
        return o instanceof Chash other && Arrays.equals(bytes, other.bytes);
    }

    @Override
    public int hashCode() {
        return Arrays.hashCode(bytes);
    }
}
