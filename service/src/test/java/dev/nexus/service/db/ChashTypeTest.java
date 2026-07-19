/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * Chash — the parse-don't-validate content-address newtype, byte[32]-backed
 * per RDR-180 (nexus-jxizy.7). The factory is the sole enforcement point for
 * the 64-lowercase-hex canonical interchange form; these tests pin every
 * rejection message a boundary 400 relies on.
 *
 * <p>POLARITY NOTE: this file REPLACES the pre-flip suite whose assertions
 * proved 64-hex was rejected and 32-hex accepted. RDR-180 inverts that
 * contract — the old tests' polarity, not their spirit, is what changed
 * (the bead spec: "REPLACED, not extended").
 */
class ChashTypeTest {

    private static final String VALID =
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
    private static final String LEGACY32 = "0123456789abcdef0123456789abcdef";

    @Test
    void fromHex_roundTrips_canonicalForm() {
        Chash c = Chash.fromHex(VALID);
        assertThat(c.toHex()).isEqualTo(VALID);
        assertThat(c.toString()).isEqualTo(VALID);
        assertThat(c.toBytes()).hasSize(Chash.BYTE_LENGTH);
    }

    @Test
    void fromHex_rejects_legacy32_withAliasResolutionHint() {
        // THE INVERSION: 32-hex was the canonical accept pre-RDR-180; now it
        // is a legacy reference. Never truncated, never padded — the message
        // must steer callers to the chash_alias resolution path.
        assertThatThrownBy(() -> Chash.fromHex(LEGACY32))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("got 32 chars")
            .hasMessageContaining("legacy 32-hex")
            .hasMessageContaining("chash_alias");
    }

    @Test
    void fromHex_rejects_otherLengths_withActualLength() {
        assertThatThrownBy(() -> Chash.fromHex("abc"))
            .hasMessageContaining("got 3 chars");
        assertThatThrownBy(() -> Chash.fromHex(""))
            .hasMessageContaining("got 0 chars");
        assertThatThrownBy(() -> Chash.fromHex(null))
            .hasMessageContaining("got null");
        assertThatThrownBy(() -> Chash.fromHex(VALID + "ab"))
            .hasMessageContaining("got 66 chars");
    }

    @Test
    void fromHex_rejects_uppercase_andNonHex_withIndex() {
        assertThatThrownBy(() -> Chash.fromHex("0123456789ABCDEF" + VALID.substring(16)))
            .hasMessageContaining("LOWERCASE")
            .hasMessageContaining("index 10");
        assertThatThrownBy(() -> Chash.fromHex("z" + VALID.substring(1)))
            .hasMessageContaining("'z' at index 0");
    }

    @Test
    void fromSha256Hex_isNowTheIdentityConstructor() {
        // Pre-RDR-180 this factory truncated to [:32]; it no longer does.
        String full = "aabbccddeeff00112233445566778899"
                    + "ffeeddccbbaa99887766554433221100";
        assertThat(Chash.fromSha256Hex(full).toHex()).isEqualTo(full);
        assertThat(Chash.fromSha256Hex(full.toUpperCase()).toHex()).isEqualTo(full);
        assertThatThrownBy(() -> Chash.fromSha256Hex(LEGACY32))
            .hasMessageContaining("FULL 64-char")
            .hasMessageContaining("32 chars");
        assertThatThrownBy(() -> Chash.fromSha256Hex(null))
            .hasMessageContaining("null");
    }

    @Test
    void ofText_isTheFullDigest_noTruncation() throws Exception {
        String text = "the amaranthine zeppelin quotient";
        var md = java.security.MessageDigest.getInstance("SHA-256");
        String fullHex = java.util.HexFormat.of().formatHex(
            md.digest(text.getBytes(java.nio.charset.StandardCharsets.UTF_8)));
        assertThat(fullHex).hasSize(64);
        assertThat(Chash.ofText(text).toHex()).isEqualTo(fullHex);
        assertThat(Chash.ofText(text)).isEqualTo(Chash.fromSha256Hex(fullHex));
    }

    @Test
    void fromSha256Bytes_wrapsExactly32_defensiveCopies() {
        byte[] digest = new byte[32];
        digest[0] = (byte) 0xab;
        Chash c = Chash.fromSha256Bytes(digest);
        digest[0] = 0;  // caller mutation must not leak in
        assertThat(c.toHex()).startsWith("ab");
        byte[] out = c.toBytes();
        out[0] = 0;     // accessor mutation must not leak back
        assertThat(c.toHex()).startsWith("ab");
        assertThatThrownBy(() -> Chash.fromSha256Bytes(new byte[16]))
            .hasMessageContaining("got 16 bytes");
        assertThatThrownBy(() -> Chash.fromSha256Bytes(null))
            .hasMessageContaining("null");
    }

    @Test
    void requireCanonical_prefixesLabel() {
        assertThatThrownBy(() -> Chash.requireCanonical(LEGACY32, "rows[3].chash"))
            .hasMessageStartingWith("rows[3].chash: ")
            .hasMessageContaining("got 32 chars");
        assertThat(Chash.requireCanonical(VALID, "x")).isEqualTo(VALID);
    }

    @Test
    void jackson_bindsThe64HexWireForm() throws Exception {
        ObjectMapper mapper = new ObjectMapper();
        Chash c = mapper.readValue('"' + VALID + '"', Chash.class);
        assertThat(c).isEqualTo(Chash.fromHex(VALID));
        assertThat(mapper.writeValueAsString(c)).isEqualTo('"' + VALID + '"');
        assertThatThrownBy(() -> mapper.readValue('"' + LEGACY32 + '"', Chash.class))
            .hasRootCauseInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void equality_isOverContents() {
        assertThat(Chash.fromHex(VALID)).isEqualTo(Chash.fromHex(VALID));
        assertThat(Chash.fromHex(VALID).hashCode()).isEqualTo(Chash.fromHex(VALID).hashCode());
        assertThat(Chash.fromHex(VALID))
            .isNotEqualTo(Chash.fromHex("f".repeat(64)));
    }
}
