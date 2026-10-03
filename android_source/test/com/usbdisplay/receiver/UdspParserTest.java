package com.usbdisplay.receiver;

import junit.framework.TestCase;
import java.util.List;
import java.util.zip.CRC32;

/** Host-runnable unit tests for UdspParser (pure Java, no Android APIs).
 * Run: scripts/test-android-parser.sh (javac + JUnit 3.8 from /usr/share/java).
 * Mirrors server/tests/frame_tests.rs behavior. */
public class UdspParserTest extends TestCase {

    static byte[] frame(int codec, int len, int seq, byte fill) {
        byte[] payload = new byte[len];
        for (int i = 0; i < len; i++) payload[i] = (byte) (fill + i * 31);
        return wire(seq, 1000 + seq, codec, 1, payload);
    }

    static byte[] wire(int seq, long pts, int codec, int flags, byte[] payload) {
        byte[] w = new byte[22 + payload.length + 4];
        putU32(w, 0, UdspParser.MAGIC);
        putU32(w, 4, seq);
        putU64(w, 8, pts);
        w[16] = (byte) codec;
        w[17] = (byte) flags;
        putU32(w, 18, payload.length);
        System.arraycopy(payload, 0, w, 22, payload.length);
        CRC32 crc = new CRC32();
        crc.update(w, 4, 18 + payload.length);
        putU32(w, 22 + payload.length, (int) crc.getValue());
        return w;
    }

    static void putU32(byte[] b, int o, int v) {
        b[o] = (byte) v;
        b[o + 1] = (byte) (v >>> 8);
        b[o + 2] = (byte) (v >>> 16);
        b[o + 3] = (byte) (v >>> 24);
    }

    static void putU64(byte[] b, int o, long v) {
        for (int i = 0; i < 8; i++) b[o + i] = (byte) (v >>> (8 * i));
    }

    static byte[] concat(byte[][] parts) {
        int n = 0;
        for (byte[] p : parts) n += p.length;
        byte[] out = new byte[n];
        int o = 0;
        for (byte[] p : parts) {
            System.arraycopy(p, 0, out, o, p.length);
            o += p.length;
        }
        return out;
    }

    public void testRoundtrip() {
        byte[] payload = "hello-jpeg".getBytes();
        byte[] w = wire(7, 1500, 1, 1, payload);
        assertEquals(0x55, w[0] & 0xFF);
        UdspParser p = new UdspParser();
        List<UdspParser.Frame> out = p.feed(w, 0, w.length);
        assertEquals(1, out.size());
        UdspParser.Frame f = out.get(0);
        assertEquals(7, f.seq);
        assertEquals(1500L, f.ptsMs);
        assertEquals(1, f.codec);
        assertEquals(new String(payload), new String(f.payload));
    }

    public void testBadMagicResync() {
        byte[] junk = new byte[]{(byte) 0xAA, (byte) 0xBB, 0x00, (byte) 0xFF, 0x55};
        byte[] stream = concat(new byte[][]{junk, frame(1, 64, 0, (byte) 3)});
        UdspParser p = new UdspParser();
        List<UdspParser.Frame> out = p.feed(stream, 0, stream.length);
        assertEquals(1, out.size());
        assertEquals(0, out.get(0).seq);
        assertTrue(p.skippedBytes >= 4);
    }

    public void testSplitHeaderAndPayload() {
        byte[] w = frame(1, 5000, 9, (byte) 4);
        UdspParser p = new UdspParser();
        assertEquals(0, p.feed(w, 0, 100).size());
        assertEquals(0, p.feed(w, 100, 3900).size());
        List<UdspParser.Frame> out = p.feed(w, 4000, w.length - 4000);
        assertEquals(1, out.size());
        assertEquals(9, out.get(0).seq);
        assertEquals(5000, out.get(0).payload.length);
    }

    public void testInvalidLength() {
        byte[] bad = wire(0, 0, 1, 1, new byte[]{0x78});
        putU32(bad, 18, 0);
        UdspParser p = new UdspParser();
        p.feed(bad, 0, bad.length);
        assertEquals(1, p.invalidLength);
        byte[] bad2 = wire(1, 0, 1, 1, new byte[]{0x79});
        putU32(bad2, 18, UdspParser.MAX_PAYLOAD_LEN + 1);
        UdspParser p2 = new UdspParser();
        p2.feed(bad2, 0, bad2.length);
        assertEquals(1, p2.invalidLength);
    }

    public void testCrcFailureThenContinue() {
        byte[] w = frame(1, 128, 4, (byte) 5);
        w[w.length - 1] ^= (byte) 0xFF;
        byte[] stream = concat(new byte[][]{w, frame(1, 128, 5, (byte) 5)});
        UdspParser p = new UdspParser();
        List<UdspParser.Frame> out = p.feed(stream, 0, stream.length);
        assertEquals(1, out.size());
        assertEquals(5, out.get(0).seq);
        assertEquals(1, p.crcMismatch);
    }

    public void testH264AcceptedUnknownRejected() {
        byte[] h = frame(2, 64, 2, (byte) 6);
        UdspParser p = new UdspParser();
        List<UdspParser.Frame> out = p.feed(h, 0, h.length);
        assertEquals(1, out.size());
        assertEquals(2, out.get(0).codec);
        byte[] bad = wire(3, 0, 0x03, 0, new byte[32]);
        byte[] stream = concat(new byte[][]{bad, frame(1, 64, 4, (byte) 6)});
        UdspParser p2 = new UdspParser();
        List<UdspParser.Frame> out2 = p2.feed(stream, 0, stream.length);
        assertEquals(1, out2.size());
        assertEquals(4, out2.get(0).seq);
        assertEquals(1, p2.invalidCodec);
    }

    public void testMultiFrameAndByteSplit() {
        byte[][] parts = new byte[10][];
        for (int s = 0; s < 10; s++) parts[s] = frame(1, 100 + s, s, (byte) 7);
        byte[] stream = concat(parts);
        UdspParser p = new UdspParser();
        List<UdspParser.Frame> out = p.feed(stream, 0, stream.length);
        assertEquals(10, out.size());
        UdspParser p2 = new UdspParser();
        List<UdspParser.Frame> all = new java.util.ArrayList<UdspParser.Frame>();
        for (int i = 0; i < stream.length; i++) all.addAll(p2.feed(stream, i, 1));
        assertEquals(10, all.size());
        for (int s = 0; s < 10; s++) assertEquals(s, all.get(s).seq);
        byte[] single = frame(1, 300, 3, (byte) 8);
        UdspParser p3 = new UdspParser();
        int n = 0;
        for (int i = 0; i < single.length; i++) n += p3.feed(single, i, 1).size();
        assertEquals(1, n);
    }

    public void testMagicInsidePayload() {
        byte[] payload = new byte[200];
        for (int i = 0; i < 200; i++) payload[i] = 0x11;
        payload[50] = 0x55;
        payload[51] = 0x44;
        payload[52] = 0x53;
        payload[53] = 0x50;
        byte[] w = wire(20, 0, 1, 1, payload);
        UdspParser p = new UdspParser();
        List<UdspParser.Frame> out = p.feed(w, 0, w.length);
        assertEquals(1, out.size());
        assertEquals(200, out.get(0).payload.length);
    }
}
