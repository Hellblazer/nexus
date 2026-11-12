// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import java.io.ByteArrayOutputStream;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * The page cutter (RDR-248 byte-cut page, bead nexus-csayy.2) on a synthetic binary COPY stream: row
 * boundaries found across arbitrary write splits, a cut at the byte target that keeps whole rows and a fresh
 * trailer, and the last row's key fields.
 */
class BinaryCopyPageTest {

    private static final byte[] SIGNATURE = {'P', 'G', 'C', 'O', 'P', 'Y', '\n', (byte) 0xff, '\r', '\n', 0};

    /** A COPY stream of rows (id int8, text), with the trailer when {@code trailer}. */
    private static byte[] stream(int rows, int textBytes, boolean trailer) {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        out.writeBytes(SIGNATURE);
        out.writeBytes(ByteBuffer.allocate(8).putInt(0).putInt(0).array());   // flags, header extension length
        for (int i = 0; i < rows; i++) {
            out.writeBytes(ByteBuffer.allocate(2).putShort((short) 2).array());
            out.writeBytes(ByteBuffer.allocate(4 + 8).putInt(8).putLong(i + 1).array());
            byte[] text = ("row-" + i + "-").repeat(Math.max(1, textBytes / 8)).getBytes(StandardCharsets.UTF_8);
            out.writeBytes(ByteBuffer.allocate(4).putInt(text.length).array());
            out.writeBytes(text);
        }
        if (trailer) out.writeBytes(ByteBuffer.allocate(2).putShort((short) -1).array());
        return out.toByteArray();
    }

    private static void feed(BinaryCopyPage page, byte[] stream, int chunk) throws Exception {
        for (int off = 0; off < stream.length; off += chunk) {
            page.write(stream, off, Math.min(chunk, stream.length - off));
        }
    }

    private static long idOf(byte[] field) {
        return ByteBuffer.wrap(field).getLong();
    }

    @Test
    void aStreamThatEndsNaturallyIsReturnedWholeAndFlaggedExhausted() throws Exception {
        byte[] stream = stream(5, 40, true);
        BinaryCopyPage page = new BinaryCopyPage(1 << 20, 100);
        feed(page, stream, 7);   // splits fall inside every field
        BinaryCopyPage.Result r = page.finish(new int[]{0});

        assertThat(r.rows()).isEqualTo(5);
        assertThat(r.exhausted()).isTrue();
        assertThat(r.body()).isEqualTo(stream);
        assertThat(idOf(r.lastKeyFields().get(0))).isEqualTo(5L);
    }

    @Test
    void aStreamThatHitsTheRowLimitIsNotExhausted() throws Exception {
        BinaryCopyPage page = new BinaryCopyPage(1 << 20, 5);
        feed(page, stream(5, 40, true), 64);
        assertThat(page.finish(new int[]{0}).exhausted()).as("5 rows of a 5-row limit may have more behind them").isFalse();
    }

    @Test
    void anEmptyCopyIsAValidEmptyPage() throws Exception {
        byte[] stream = stream(0, 0, true);
        BinaryCopyPage page = new BinaryCopyPage(1 << 20, 100);
        feed(page, stream, 3);
        BinaryCopyPage.Result r = page.finish(new int[]{0});
        assertThat(r.rows()).isZero();
        assertThat(r.lastKeyFields()).isEmpty();
        assertThat(r.exhausted()).isTrue();
        assertThat(r.body()).isEqualTo(stream);
    }

    @Test
    void theCutKeepsWholeRowsAndAddsATrailer() throws Exception {
        // rows of about 130 bytes, target 1000: the cut lands inside a row's bytes; the page ends at the row before
        byte[] stream = stream(50, 100, false);
        BinaryCopyPage page = new BinaryCopyPage(1000, 10_000);
        assertThatThrownBy(() -> feed(page, stream, 50)).isInstanceOf(BinaryCopyPage.PageFull.class);
        BinaryCopyPage.Result r = page.finish(new int[]{0});

        assertThat(r.exhausted()).isFalse();
        assertThat(r.rows()).isBetween(5, 12);
        assertThat(idOf(r.lastKeyFields().get(0))).isEqualTo(r.rows());
        byte[] body = r.body();
        assertThat(body[body.length - 2]).isEqualTo((byte) 0xff);
        assertThat(body[body.length - 1]).isEqualTo((byte) 0xff);
        // the body is the stream's first rows, byte for byte, then the trailer: it parses as a whole stream
        BinaryCopyPage again = new BinaryCopyPage(1 << 20, 10_000);
        feed(again, body, 1000);
        BinaryCopyPage.Result reparsed = again.finish(new int[]{0});
        assertThat(reparsed.rows()).isEqualTo(r.rows());
        assertThat(reparsed.exhausted()).isTrue();
        assertThat(body.length).as("a page overshoots its target by less than one write plus one row").isLessThan(1000 + 50 + 140);
    }

    @Test
    void aNullKeyFieldIsRefused() throws Exception {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        out.writeBytes(SIGNATURE);
        out.writeBytes(ByteBuffer.allocate(8).putInt(0).putInt(0).array());
        out.writeBytes(ByteBuffer.allocate(2 + 4).putShort((short) 1).putInt(-1).array());
        out.writeBytes(ByteBuffer.allocate(2).putShort((short) -1).array());
        BinaryCopyPage page = new BinaryCopyPage(1 << 20, 100);
        feed(page, out.toByteArray(), 100);
        assertThatThrownBy(() -> page.finish(new int[]{0})).isInstanceOf(IllegalStateException.class)
            .hasMessageContaining("NULL");
    }

    @Test
    void aStreamWithABadSignatureIsRefused() {
        BinaryCopyPage page = new BinaryCopyPage(1 << 20, 100);
        byte[] junk = new byte[64];
        assertThatThrownBy(() -> page.write(junk, 0, junk.length)).isInstanceOf(IllegalStateException.class)
            .hasMessageContaining("signature");
    }

    @Test
    void rowsOfSeveralKeyColumnsReturnEachKeyFieldInOrder() throws Exception {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        out.writeBytes(SIGNATURE);
        out.writeBytes(ByteBuffer.allocate(8).putInt(0).putInt(0).array());
        byte[] a = "tenant".getBytes(StandardCharsets.UTF_8);
        byte[] b = {1, 2, 3};
        out.writeBytes(ByteBuffer.allocate(2).putShort((short) 3).array());
        out.writeBytes(ByteBuffer.allocate(4).putInt(a.length).array());
        out.writeBytes(a);
        out.writeBytes(ByteBuffer.allocate(4).putInt(-1).array());   // a NULL non-key column
        out.writeBytes(ByteBuffer.allocate(4).putInt(b.length).array());
        out.writeBytes(b);
        out.writeBytes(ByteBuffer.allocate(2).putShort((short) -1).array());
        BinaryCopyPage page = new BinaryCopyPage(1 << 20, 100);
        feed(page, out.toByteArray(), 5);
        BinaryCopyPage.Result r = page.finish(new int[]{0, 2});
        assertThat(r.lastKeyFields()).hasSize(2);
        assertThat(new String(r.lastKeyFields().get(0), StandardCharsets.UTF_8)).isEqualTo("tenant");
        assertThat(r.lastKeyFields().get(1)).isEqualTo(b);
        assertThat(r.rows()).isEqualTo(1);
    }
}
