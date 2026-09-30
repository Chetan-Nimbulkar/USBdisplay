"""Wire protocol shared with the Android USBDisplay receiver."""

from __future__ import annotations

import struct
import zlib

MAGIC = b"UDSP"
HEADER = struct.Struct("<4sIQBBI")
CRC = struct.Struct("<I")

CODEC_MJPEG = 1
CODEC_H264 = 2
MAX_PAYLOAD_LEN = 8 * 1024 * 1024


def encode_frame(
    payload: bytes,
    sequence: int,
    pts_ms: int,
    codec: int = CODEC_MJPEG,
    flags: int = 0,
) -> bytes:
    """Encode one frame exactly as ``UdspParser`` in the APK expects it."""
    if not payload:
        raise ValueError("payload must not be empty")
    if len(payload) > MAX_PAYLOAD_LEN:
        raise ValueError(f"payload exceeds {MAX_PAYLOAD_LEN} bytes")
    if codec not in (CODEC_MJPEG, CODEC_H264):
        raise ValueError(f"unsupported codec: {codec}")

    header = HEADER.pack(
        MAGIC,
        sequence & 0xFFFFFFFF,
        pts_ms & 0xFFFFFFFFFFFFFFFF,
        codec,
        flags & 0xFF,
        len(payload),
    )
    checksum = zlib.crc32(header[4:])
    checksum = zlib.crc32(payload, checksum) & 0xFFFFFFFF
    return header + payload + CRC.pack(checksum)
