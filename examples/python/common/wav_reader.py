"""Tiny RIFF reader for the WAV codecs the deepfake service consumes.

Six codecs supported today; wave format tag + bits_per_sample → wire codec:

  - `S16LE` — 16-bit signed linear PCM,      LE (WAVE_FORMAT_PCM 0x0001, 16-bit)
  - `S24LE` — 24-bit signed linear PCM,      LE (WAVE_FORMAT_PCM 0x0001, 24-bit)
  - `S32LE` — 32-bit signed linear PCM,      LE (WAVE_FORMAT_PCM 0x0001, 32-bit)
  - `F32LE` — 32-bit IEEE-float PCM,         LE (WAVE_FORMAT_IEEE_FLOAT 0x0003)
  - `PCMU`  — G.711 μ-law,  8-bit               (WAVE_FORMAT_MULAW 0x0007)
  - `PCMA`  — G.711 A-law,  8-bit               (WAVE_FORMAT_ALAW  0x0006)

Envelope-transparent:
  - WAVE_FORMAT_EXTENSIBLE (0xFFFE) — the extension chunk carries a
    SubFormat GUID whose first 4 bytes are the "real" format tag. We
    verify the GUID suffix matches Microsoft's KSDATAFORMAT_SUBTYPE_*
    pattern, then dispatch as if the file used that tag directly.

Design note: all six codecs pass through as raw wire bytes. Client-side
conversion (e.g. int24 → float32) is deliberately avoided — the deepfake
service decodes every codec natively via vectorised numpy, so every
ingress (Python client, TypeScript client, aurigin-router, future SDKs)
gets the same fast path for free. Keeps the wire ~25% smaller for
int24 inputs vs a client-side up-convert to F32LE.

Used by client.py + phone_call.py + phone_call_burst.py — the WAV reader
is the one piece of "I/O glue" all three examples share. In a real
FreeSWITCH / Twilio Media Stream / SIPREC integration, this file is
where you'd swap to your own socket / fork reader; the rest of the
examples stay the same.

Mirrored in examples/typescript/common/wav_reader.ts.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# WAVE format tags per Microsoft's RIFF spec. Anything not listed here
# (WMA, ADPCM, MP3-in-WAV, …) raises ValueError.
_WAVE_FORMAT_PCM        = 0x0001
_WAVE_FORMAT_IEEE_FLOAT = 0x0003
_WAVE_FORMAT_ALAW       = 0x0006
_WAVE_FORMAT_MULAW      = 0x0007
_WAVE_FORMAT_EXTENSIBLE = 0xFFFE

# KSDATAFORMAT_SUBTYPE_* GUID suffix. Microsoft's standard EXTENSIBLE
# sub-format GUIDs follow the pattern (canonical GUID string form):
#   xxxxxxxx-0000-0010-8000-00aa00389b71
# where xxxxxxxx is the format tag as an 8-hex-digit uint32.
#
# On the wire, GUIDs use Microsoft's mixed-endianness layout:
#   - first 4 bytes:  little-endian uint32   ← format tag
#   - next 2 bytes:   little-endian uint16   ← "0000"
#   - next 2 bytes:   little-endian uint16   ← "0010" → 10 00 on the wire
#   - last 8 bytes:   big-endian (raw)       ← 80 00 00 aa 00 38 9b 71
#
# So we read the first 4 bytes as the "real" format tag and check the
# remaining 12 bytes match this constant.
_KSDATAFORMAT_SUBTYPE_SUFFIX = bytes.fromhex("00001000800000aa00389b71")

# (format tag, bits) → (wire_format string, bytes-per-sample). The wire
# format string matches AudioBuffer.format (legacy 0.2.x wire); the codec
# enum for AudioFrame is resolved on demand via WavData.audio_codec.
_FORMAT_TABLE: dict[tuple[int, int], tuple[str, int]] = {
    (_WAVE_FORMAT_PCM,        16): ("S16LE", 2),
    (_WAVE_FORMAT_PCM,        24): ("S24LE", 3),
    (_WAVE_FORMAT_PCM,        32): ("S32LE", 4),
    (_WAVE_FORMAT_IEEE_FLOAT, 32): ("F32LE", 4),
    (_WAVE_FORMAT_MULAW,       8): ("PCMU",  1),
    (_WAVE_FORMAT_ALAW,        8): ("PCMA",  1),
}


@dataclass(frozen=True)
class WavData:
    """A WAV file's data chunk + the metadata the gRPC audio message needs.

    `wire_format` is the value that goes straight into `AudioBuffer.format`
    — one of "S16LE" / "S24LE" / "S32LE" / "F32LE" / "PCMU" / "PCMA" —
    matching the deepfake-service decoder's vocabulary for the legacy
    Twilio-vendored AudioBuffer wire.

    `audio_codec` is the equivalent AudioCodec enum value for building
    `aurigin.media.v1.AudioFrame` messages (the new-in-0.3.0 wire shape).
    """
    samples: bytes
    rate: int
    channels: int
    wire_format: str  # "S16LE" | "S24LE" | "S32LE" | "F32LE" | "PCMU" | "PCMA"
    _bytes_per_frame_sample: int = 2  # width in bytes of a single sample (per-channel)

    @property
    def bytes_per_sample(self) -> int:
        """Bytes per audio frame (sample-width × channels)."""
        return self._bytes_per_frame_sample * self.channels

    @property
    def duration_s(self) -> float:
        denom = self.rate * self.bytes_per_sample
        return len(self.samples) / denom if denom else 0.0

    @property
    def audio_codec(self) -> int:
        """AudioCodec enum value matching `wire_format`, for building AudioFrame.

        Lazy import so pure AudioBuffer-only consumers of this module don't
        drag in the aurigin.media.v1 stubs (relevant for older client code
        pinned to aurigin-protos 0.2.x).
        """
        from aurigin.media.v1 import audio_frame_pb2 as af_pb
        return {
            "S16LE": af_pb.AUDIO_CODEC_S16LE,
            "S24LE": af_pb.AUDIO_CODEC_S24LE,
            "S32LE": af_pb.AUDIO_CODEC_S32LE,
            "F32LE": af_pb.AUDIO_CODEC_F32LE,
            "PCMU":  af_pb.AUDIO_CODEC_PCMU,
            "PCMA":  af_pb.AUDIO_CODEC_PCMA,
        }[self.wire_format]


def _resolve_extensible(fmt_body: bytes) -> int:
    """Extract the real format tag from a WAVE_FORMAT_EXTENSIBLE fmt chunk.

    `fmt_body` is the fmt chunk payload starting at the format tag (i.e.
    the 2 bytes at raw offset `fmt_chunk_start + 8`). Returns the format
    tag encoded in the first 4 bytes of the SubFormat GUID, provided the
    remaining 12 bytes match Microsoft's KSDATAFORMAT_SUBTYPE_* suffix.
    Raises ValueError if the GUID doesn't match — that catches non-Microsoft
    EXTENSIBLE subformats we don't recognise.
    """
    # fmt body layout (offsets relative to `fmt_body`):
    #   0    format tag (2)       — 0xFFFE
    #   2    channels (2)
    #   4    sample rate (4)
    #   8    byte rate (4)
    #  12    block align (2)
    #  14    bits per sample (2)
    #  16    cb_size (2)          — 22 for EXTENSIBLE
    #  18    valid bits per sample (2)
    #  20    channel mask (4)
    #  24    SubFormat GUID (16)
    if len(fmt_body) < 40:
        raise ValueError(
            f"EXTENSIBLE fmt chunk too short (got {len(fmt_body)} bytes, need ≥ 40)",
        )
    guid = fmt_body[24:40]
    if guid[4:16] != _KSDATAFORMAT_SUBTYPE_SUFFIX:
        raise ValueError(
            f"EXTENSIBLE SubFormat GUID {guid.hex()} is not a "
            f"KSDATAFORMAT_SUBTYPE_* — unsupported vendor sub-format",
        )
    return int.from_bytes(guid[:4], "little")


def read_wav(path: Path) -> WavData:
    """Parse a RIFF/WAVE file into a WavData.

    Raises ValueError on:
      - non-RIFF/WAVE files (mislabeled .wav, e.g. an MP3)
      - missing data chunk
      - unsupported (format tag, bit depth) combos
      - EXTENSIBLE with a non-Microsoft-standard SubFormat GUID
    """
    buf = path.read_bytes()
    if buf[:4] != b"RIFF" or buf[8:12] != b"WAVE":
        raise ValueError(f"{path.name}: not a RIFF/WAVE file")

    offset = 12
    audio_format = channels = bits_per_sample = 0
    rate = 0
    fmt_body = b""
    data_start = -1
    data_len = 0
    while offset + 8 <= len(buf):
        chunk_id = buf[offset : offset + 4]
        size = int.from_bytes(buf[offset + 4 : offset + 8], "little")
        if chunk_id == b"fmt ":
            fmt_body = buf[offset + 8 : offset + 8 + size]
            audio_format = int.from_bytes(fmt_body[0:2], "little")
            channels = int.from_bytes(fmt_body[2:4], "little")
            rate = int.from_bytes(fmt_body[4:8], "little")
            bits_per_sample = int.from_bytes(fmt_body[14:16], "little")
        elif chunk_id == b"data":
            data_start = offset + 8
            data_len = size
            break
        offset += 8 + size + (size & 1)  # chunks are word-aligned

    if data_start < 0:
        raise ValueError(f"{path.name}: no data chunk")

    # EXTENSIBLE: unwrap the SubFormat GUID and keep going with the real tag.
    # bits_per_sample and channels stay the same — EXTENSIBLE only changes
    # the codec identifier, not the sample width or layout.
    if audio_format == _WAVE_FORMAT_EXTENSIBLE:
        try:
            audio_format = _resolve_extensible(fmt_body)
        except ValueError as e:
            raise ValueError(f"{path.name}: {e}") from None

    key = (audio_format, bits_per_sample)
    if key not in _FORMAT_TABLE:
        raise ValueError(
            f"{path.name}: unsupported WAV (format tag 0x{audio_format:04x}, "
            f"{bits_per_sample}-bit) — expected one of: "
            f"16/24/32-bit PCM / 32-bit IEEE float / 8-bit μ-law / 8-bit A-law",
        )
    wire_format, bytes_per_frame_sample = _FORMAT_TABLE[key]
    return WavData(
        samples=buf[data_start : data_start + data_len],
        rate=rate, channels=channels, wire_format=wire_format,
        _bytes_per_frame_sample=bytes_per_frame_sample,
    )
