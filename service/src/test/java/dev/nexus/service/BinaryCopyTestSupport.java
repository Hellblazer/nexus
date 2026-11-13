// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import java.io.ByteArrayOutputStream;
import java.nio.ByteBuffer;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/**
 * Reads and writes PostgreSQL's binary COPY stream (RDR-248 import tests, bead nexus-csayy.6): a page the export
 * wrote is cut into parts at row boundaries, and a test makes an "older" or "newer" file by dropping or adding a
 * field in every row. Format: an 11-byte signature, a 32-bit flags word, a 32-bit extension length and its bytes;
 * then per row a 16-bit field count and per field a 32-bit length (-1 for NULL) and its bytes; then a 16-bit -1.
 */
final class BinaryCopyTestSupport {

    private BinaryCopyTestSupport() {
    }

    private static final byte[] SIGNATURE = {'P', 'G', 'C', 'O', 'P', 'Y', '\n', (byte) 0xff, '\r', '\n', 0};

    /** One row: its fields, null for a NULL. */
    record Row(List<byte[]> fields) {
    }

    /** A parsed stream: the header bytes up to the first row, and the rows. */
    record Stream(byte[] header, List<Row> rows) {
    }

    static Stream parse(byte[] page) {
        ByteBuffer in = ByteBuffer.wrap(page);
        byte[] sig = new byte[SIGNATURE.length];
        in.get(sig);
        if (!Arrays.equals(sig, SIGNATURE)) throw new IllegalArgumentException("not a binary COPY stream");
        in.getInt();
        int ext = in.getInt();
        in.position(in.position() + ext);
        byte[] header = Arrays.copyOfRange(page, 0, in.position());
        List<Row> rows = new ArrayList<>();
        while (true) {
            short count = in.getShort();
            if (count == -1) break;
            List<byte[]> fields = new ArrayList<>();
            for (int i = 0; i < count; i++) {
                int len = in.getInt();
                if (len == -1) {
                    fields.add(null);
                } else {
                    byte[] f = new byte[len];
                    in.get(f);
                    fields.add(f);
                }
            }
            rows.add(new Row(fields));
        }
        return new Stream(header, rows);
    }

    static byte[] encode(Stream s) {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        out.writeBytes(s.header());
        for (Row r : s.rows()) {
            ByteBuffer b = ByteBuffer.allocate(2);
            b.putShort((short) r.fields().size());
            out.writeBytes(b.array());
            for (byte[] f : r.fields()) {
                ByteBuffer l = ByteBuffer.allocate(4);
                l.putInt(f == null ? -1 : f.length);
                out.writeBytes(l.array());
                if (f != null) out.writeBytes(f);
            }
        }
        out.writeBytes(new byte[] {(byte) 0xff, (byte) 0xff});
        return out.toByteArray();
    }

    /** The page cut into streams of at most {@code rowsPerPart} rows (an empty page is one empty stream). */
    static List<byte[]> split(byte[] page, int rowsPerPart) {
        Stream s = parse(page);
        List<byte[]> parts = new ArrayList<>();
        if (s.rows().isEmpty()) {
            parts.add(encode(s));
            return parts;
        }
        for (int i = 0; i < s.rows().size(); i += rowsPerPart) {
            parts.add(encode(new Stream(s.header(), s.rows().subList(i, Math.min(s.rows().size(), i + rowsPerPart)))));
        }
        return parts;
    }

    static int rowCount(byte[] page) {
        return parse(page).rows().size();
    }

    /** The page without field {@code index} in every row. */
    static byte[] dropField(byte[] page, int index) {
        Stream s = parse(page);
        List<Row> rows = new ArrayList<>();
        for (Row r : s.rows()) {
            List<byte[]> f = new ArrayList<>(r.fields());
            f.remove(index);
            rows.add(new Row(f));
        }
        return encode(new Stream(s.header(), rows));
    }

    /** The page with one more field at the end of every row, {@code value} (null for NULL). */
    static byte[] appendField(byte[] page, byte[] value) {
        Stream s = parse(page);
        List<Row> rows = new ArrayList<>();
        for (Row r : s.rows()) {
            List<byte[]> f = new ArrayList<>(r.fields());
            f.add(value);
            rows.add(new Row(f));
        }
        return encode(new Stream(s.header(), rows));
    }
}
