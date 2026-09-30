import struct
import unittest
import zlib

from bridge.protocol import CODEC_MJPEG, encode_frame


class ProtocolTests(unittest.TestCase):
    def test_packet_matches_android_layout_and_crc(self) -> None:
        payload = b"\xff\xd8jpeg\xff\xd9"
        packet = encode_frame(payload, sequence=0x10203040, pts_ms=1234)

        self.assertEqual(packet[:4], b"UDSP")
        self.assertEqual(struct.unpack_from("<I", packet, 4)[0], 0x10203040)
        self.assertEqual(struct.unpack_from("<Q", packet, 8)[0], 1234)
        self.assertEqual(packet[16], CODEC_MJPEG)
        self.assertEqual(packet[17], 0)
        self.assertEqual(struct.unpack_from("<I", packet, 18)[0], len(payload))
        self.assertEqual(packet[22:-4], payload)
        self.assertEqual(
            struct.unpack_from("<I", packet, len(packet) - 4)[0],
            zlib.crc32(packet[4:-4]) & 0xFFFFFFFF,
        )

    def test_rejects_empty_payload(self) -> None:
        with self.assertRaises(ValueError):
            encode_frame(b"", sequence=0, pts_ms=0)


if __name__ == "__main__":
    unittest.main()
