package com.usbdisplay.receiver;

import java.util.ArrayList;
import java.util.List;
import java.util.zip.CRC32;

/**
 * UDSP/1 streaming parser. Mirrors server/src/frame.rs and docs/PROTOCOL.md.
 * Layout (LE): MAGIC u32 | SEQ u32 | PTS u64 | CODEC u8 | FLAGS u8 | LEN u32 | PAYLOAD | CRC u32.
 * Only CODEC_MJPEG (0x01) accepted; H.264 (0x02) reserved/rejected.
 * feed() never throws on hostile input; length validated before allocation.
 */
public final class UdspParser {
    public static final int MAGIC = 0x50534455;
    public static final int HEADER_LEN = 22;
    public static final int MAX_PAYLOAD_LEN = 8 * 1024 * 1024;
    public static final int CODEC_MJPEG = 0x01;
    /** H.264 Annex-B access units (PROTOCOL.md §9). Accepted since 2.7. */
    public static final int CODEC_H264 = 0x02;

    public static boolean codecAccepted(int codec) {
        return codec == CODEC_MJPEG || codec == CODEC_H264;
    }

    public static final class Frame {
        public int seq;
        public long ptsMs;
        public int codec;
        public int flags;
        public byte[] payload;
    }

    /** Non-fatal stream events. Counts only; parsing continues. */
    public long invalidLength;
    public long invalidCodec;
    public long crcMismatch;
    public long skippedBytes;

    private byte[] buf = new byte[65536 + 32];
    private int count; // valid bytes in buf

    private static int u32le(byte[] b, int o) {
        return (b[o] & 0xFF) | ((b[o + 1] & 0xFF) << 8) | ((b[o + 2] & 0xFF) << 16) | ((b[o + 3] & 0xFF) << 24);
    }

    private static long u64le(byte[] b, int o) {
        long v = 0;
        for (int i = 7; i >= 0; i--) v = (v << 8) | (b[o + i] & 0xFF);
        return v;
    }

    private void ensure(int extra) {
        if (count + extra <= buf.length) return;
        int nlen = Math.max(buf.length * 2, count + extra);
        byte[] nb = new byte[nlen];
        System.arraycopy(buf, 0, nb, 0, count);
        buf = nb;
    }

    private void consume(int n) {
        int rest = count - n;
        if (rest > 0) System.arraycopy(buf, n, buf, 0, rest);
        count = rest;
    }

    /** Bytes of an unfinished tail held between feeds (0 at clean EOF). */
    public int buffered() {
        return count;
    }

    public List<Frame> feed(byte[] data, int off, int len) {
        ensure(len);
        System.arraycopy(data, off, buf, count, len);
        count += len;
        List<Frame> out = new ArrayList<Frame>();
        CRC32 crc = new CRC32();
        for (;;) {
            if (count < 4) break;
            int start = -1;
            for (int i = 0; i <= count - 4; i++) {
                if (u32le(buf, i) == MAGIC) {
                    start = i;
                    break;
                }
            }
            if (start < 0) {
                int keep = Math.min(count, 3);
                skippedBytes += (count - keep);
                consume(count - keep);
                break;
            }
            if (start > 0) {
                skippedBytes += start;
                consume(start);
            }
            if (count < HEADER_LEN) break;
            long llen = u32le(buf, 18) & 0xFFFFFFFFL;
            if (llen == 0 || llen > MAX_PAYLOAD_LEN) {
                invalidLength++;
                consume(4); // drop MAGIC, rescan
                continue;
            }
            int ilen = (int) llen;
            long total = (long) HEADER_LEN + ilen + 4;
            if (count < total) break; // split frame: wait
            int codec = buf[16] & 0xFF;
            if (!codecAccepted(codec)) {
                invalidCodec++;
                consume((int) total);
                continue;
            }
            crc.reset();
            crc.update(buf, 4, HEADER_LEN - 4 + ilen);
            int actual = (int) crc.getValue();
            int expected = u32le(buf, HEADER_LEN + ilen);
            if (actual != expected) {
                crcMismatch++;
                consume(1); // resync past MAGIC
                continue;
            }
            Frame f = new Frame();
            f.seq = u32le(buf, 4);
            f.ptsMs = u64le(buf, 8);
            f.codec = codec;
            f.flags = buf[17] & 0xFF;
            f.payload = new byte[ilen];
            System.arraycopy(buf, HEADER_LEN, f.payload, 0, ilen);
            consume((int) total);
            out.add(f);
        }
        return out;
    }
}
