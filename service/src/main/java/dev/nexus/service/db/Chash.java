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
 * Content address of a chunk — parse-don't-validate newtype (nexus-z4skl).
 *
 * <p>The canonical chash today is {@code sha256(chunk_text)[:32]} rendered as
 * 32 LOWERCASE hex chars (128 bits). It is load-bearing across five tables
 * (catalog_document_chunks, chunks_384/768/1024, chash_index) yet was modelled
 * everywhere as a bare {@code String}, so nothing at the HTTP boundary
 * distinguished a content hash from arbitrary text. The most natural caller
 * mistake — passing the FULL 64-char sha256 hex — sailed through every handler
 * and only tripped the DB {@code CHECK(length(chash)=32)} deep inside a
 * per-row transaction, where batch writers swallowed it into
 * {@code failed_doc_ids} with no reason (cost 3 deploy-gate iterations on the
 * v0.1.24 batch-endpoint probe, 2026-07-04).
 *
 * <p>This type's factories are the sole enforcement point: parse at the
 * boundary, get a uniform 400 with the offending length BEFORE any
 * transaction; the DB CHECK demotes to belt-and-suspenders.
 *
 * <p>Representation: {@code byte[16]} — the type holds bytes, the world sees
 * hex ({@link #toHex()} / {@code @JsonValue}). RDR-180 (draft) proposes moving
 * storage to the full 32-byte digest; when that is blessed, this type's width
 * constants flip 16→32 and {@link #fromSha256Hex} stops truncating — callers
 * are already insulated.
 */
public final class Chash {

    /** Canonical interchange width: 32 lowercase hex chars (128 bits). */
    public static final int HEX_LENGTH = 32;
    /** Internal width: 16 bytes. */
    public static final int BYTE_LENGTH = 16;

    private static final HexFormat HEX = HexFormat.of();  // lowercase by default

    private final byte[] bytes;

    private Chash(byte[] bytes) {
        this.bytes = bytes;
    }

    /**
     * Parse the canonical 32-lowercase-hex form. The single validation
     * chokepoint — error messages carry the ACTUAL length so the classic
     * 64-char (full sha256) mistake is self-diagnosing at the boundary.
     *
     * @throws IllegalArgumentException on null, wrong length, uppercase or
     *         non-hex input.
     */
    @JsonCreator
    public static Chash fromHex(String hex) {
        if (hex == null) {
            throw new IllegalArgumentException(
                "invalid chash: expected " + HEX_LENGTH
                + " lowercase hex chars (sha256[:32] content address), got null");
        }
        if (hex.length() != HEX_LENGTH) {
            String hint = hex.length() == 64
                ? " — a full sha256 hex? the canonical chash is its [:32] prefix"
                    + " (use Chash.fromSha256Hex to truncate deliberately)"
                : "";
            throw new IllegalArgumentException(
                "invalid chash: expected " + HEX_LENGTH
                + " lowercase hex chars (sha256[:32] content address), got "
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
     * The named home for the {@code [:32]} truncation: accept a FULL 64-char
     * sha256 hex and derive the canonical chash from its prefix. Existence of
     * this factory is what removes the implicit-truncation ambiguity that
     * spawns bare-64 callers.
     */
    public static Chash fromSha256Hex(String fullSha256Hex) {
        if (fullSha256Hex == null || fullSha256Hex.length() != 64) {
            throw new IllegalArgumentException(
                "fromSha256Hex expects the FULL 64-char sha256 hex, got "
                + (fullSha256Hex == null ? "null" : fullSha256Hex.length() + " chars"));
        }
        return fromHex(fullSha256Hex.substring(0, HEX_LENGTH).toLowerCase());
    }

    /** Compute the canonical chash of chunk text (sha256 over UTF-8, [:32]). */
    public static Chash ofText(String chunkText) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            byte[] digest = md.digest(chunkText.getBytes(StandardCharsets.UTF_8));
            return new Chash(Arrays.copyOf(digest, BYTE_LENGTH));
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 unavailable", e);  // JVM-guaranteed algorithm
        }
    }

    /** Canonical 32-char lowercase hex rendering (the wire/storage form). */
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
