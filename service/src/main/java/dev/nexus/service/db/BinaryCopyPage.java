// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.io.IOException;
import java.io.OutputStream;
import java.nio.ByteBuffer;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/**
 * Collects one export page from a PostgreSQL binary {@code COPY ... TO STDOUT} stream and cuts it by bytes
 * (RDR-248, bead nexus-csayy.2). The stream is the driver's {@code CopyManager.copyOut} sink: this class
 * walks the binary format as the bytes arrive, so it always knows where the last COMPLETE row ends, and
 * once that offset reaches {@code targetBytes} it throws {@link PageFull}, which makes the driver cancel
 * the copy. The page is then the header, every complete row so far, and a fresh trailer: a valid binary
 * COPY stream of its own, so a client can feed each page to {@code COPY ... FROM STDIN (FORMAT binary)}
 * as it is.
 *
 * <p>Binary COPY format (PostgreSQL docs, COPY, "Binary Format"): an 11-byte signature, an int32 flags
 * word, an int32 header-extension length and that many bytes; then per row an int16 field count and per
 * field an int32 length (-1 is NULL) and the bytes; the trailer is an int16 of -1. All integers are
 * big-endian.
 *
 * <p>The page overshoots {@code targetBytes} by at most one network read plus one row, because the cut is
 * tested after each write. Not thread-safe: one page, one thread.
 */
final class BinaryCopyPage extends OutputStream {

    /** The copy was cut at the byte target; thrown out of {@link #write} so the driver cancels the copy. */
    static final class PageFull extends IOException {
        PageFull() {
            super("export page reached its byte target", null);
        }

        @Override
        public synchronized Throwable fillInStackTrace() {
            return this;
        }
    }

    /**
     * One finished page: its bytes are {@code buffer[0, length)}, a whole binary COPY stream (the buffer is the
     * page's own, handed over without a copy; it may be longer than {@code length}). {@code exhausted} is true
     * when the copy ran to its end with fewer rows than the limit.
     */
    record Result(byte[] buffer, int length, int rows, List<byte[]> lastKeyFields, boolean exhausted) {
        /** A copy of the page's bytes; the routes write the buffer directly, so this is for tests. */
        byte[] body() {
            return Arrays.copyOf(buffer, length);
        }
    }

    private static final int SIGNATURE_LENGTH = 11;
    private static final int PREFIX_LENGTH = SIGNATURE_LENGTH + 4 + 4;
    private static final byte[] SIGNATURE = {'P', 'G', 'C', 'O', 'P', 'Y', '\n', (byte) 0xff, '\r', '\n', 0};

    private final int targetBytes;
    private final int maxRows;
    private byte[] buf = new byte[1 << 16];
    private int len;
    private int headerEnd = -1;
    private int scanPos;
    private int endOfRows;
    private int lastRowStart = -1;
    private int rows;
    private boolean sawTrailer;

    BinaryCopyPage(int targetBytes, int maxRows) {
        this.targetBytes = targetBytes;
        this.maxRows = maxRows;
    }

    @Override
    public void write(int b) throws IOException {
        write(new byte[]{(byte) b}, 0, 1);
    }

    @Override
    public void write(byte[] src, int off, int count) throws IOException {
        if (len + count > buf.length) {
            // double, but never past the target plus a row's worth: one allocation near the target, not two
            buf = Arrays.copyOf(buf, Math.max(len + count, Math.min(buf.length * 2, targetBytes + (1 << 20))));
        }
        System.arraycopy(src, off, buf, len, count);
        len += count;
        advance();
        if (!sawTrailer && headerEnd >= 0 && endOfRows >= targetBytes) {
            throw new PageFull();
        }
    }

    private void advance() {
        if (headerEnd < 0) {
            if (len < PREFIX_LENGTH) return;
            for (int i = 0; i < SIGNATURE_LENGTH; i++) {
                if (buf[i] != SIGNATURE[i]) {
                    throw new IllegalStateException("not a binary COPY stream: bad signature");
                }
            }
            int extension = ByteBuffer.wrap(buf, SIGNATURE_LENGTH + 4, 4).getInt();
            if (extension < 0 || extension > (1 << 20)) {
                throw new IllegalStateException("binary COPY header extension length " + extension);
            }
            if (len < PREFIX_LENGTH + extension) return;
            headerEnd = PREFIX_LENGTH + extension;
            scanPos = headerEnd;
            endOfRows = headerEnd;
        }
        while (!sawTrailer && len - scanPos >= 2) {
            int fields = ByteBuffer.wrap(buf, scanPos, 2).getShort();
            if (fields == -1) {
                sawTrailer = true;
                return;
            }
            int p = scanPos + 2;
            boolean complete = true;
            for (int i = 0; i < fields; i++) {
                if (len - p < 4) {
                    complete = false;
                    break;
                }
                int fieldLen = ByteBuffer.wrap(buf, p, 4).getInt();
                p += 4;
                if (fieldLen >= 0) {
                    if (len - p < fieldLen) {
                        complete = false;
                        break;
                    }
                    p += fieldLen;
                }
            }
            if (!complete) return;
            lastRowStart = scanPos;
            scanPos = p;
            endOfRows = p;
            rows++;
        }
    }

    /**
     * The page: header, whole rows and a fresh trailer; {@code keyIndexes} are the positions of the primary
     * key's columns in the row, and {@code lastKeyFields} the bytes of those fields in the last row (empty
     * when the page holds no row).
     */
    Result finish(int[] keyIndexes) {
        if (headerEnd < 0) {
            throw new IllegalStateException("the binary COPY stream ended before its header was complete");
        }
        if (buf.length < endOfRows + 2) {
            buf = Arrays.copyOf(buf, endOfRows + 2);
        }
        buf[endOfRows] = (byte) 0xff;
        buf[endOfRows + 1] = (byte) 0xff;
        int length = endOfRows + 2;
        List<byte[]> key = new ArrayList<>();
        if (rows > 0) {
            int fields = ByteBuffer.wrap(buf, lastRowStart, 2).getShort();
            List<byte[]> all = new ArrayList<>(fields);
            int p = lastRowStart + 2;
            for (int i = 0; i < fields; i++) {
                int fieldLen = ByteBuffer.wrap(buf, p, 4).getInt();
                p += 4;
                if (fieldLen < 0) {
                    all.add(null);
                } else {
                    all.add(Arrays.copyOfRange(buf, p, p + fieldLen));
                    p += fieldLen;
                }
            }
            for (int index : keyIndexes) {
                byte[] field = all.get(index);
                if (field == null) {
                    throw new IllegalStateException("a primary-key field is NULL in the last exported row");
                }
                key.add(field);
            }
        }
        return new Result(buf, length, rows, key, sawTrailer && rows < maxRows);
    }
}
