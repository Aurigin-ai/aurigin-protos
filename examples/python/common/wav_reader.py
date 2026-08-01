"""Tiny RIFF reader supporting linear PCM + IEEE-float + G.711 μ-law/A-law WAVs.

The stdlib `wave` module rejects anything that isn't 16-bit PCM (raises on
float and μ/A-law format tags), so we parse RIFF ourselves and dispatch the
audio_format tag to the wire formats the deepfake-service decoder accepts:

  - `S16LE` — 16-bit signed linear PCM, little-endian (WAVE_FORMAT_PCM 0x0001)
  - `F32LE` — 32-bit IEEE-float PCM,   little-endian (WAVE_FORMAT_IEEE_FLOAT 0x0003)
  - `PCMU`  — G.711 μ-law, 8-bit                     (WAVE_FORMAT_MULAW 0x0007)
  - `PCMA`  — G.711 A-law, 8-bit                     (WAVE_FORMAT_ALAW  0x0006)

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

# WAVE format tags per Microsoft's RIFF spec. Anything else (ADPCM, WMA, …)
# raises ValueError — the deepfake-service decoder only accepts the codecs
# listed below.
_WAVE_FORMAT_PCM        = 0x0001
_WAVE_FORMAT_IEEE_FLOAT = 0x0003
_WAVE_FORMAT_ALAW       = 0x0006
_WAVE_FORMAT_MULAW      = 0x0007

# Format-tag → (wire_format string, bytes_per_sample) dispatch. The wire
# format string matches AudioBuffer.format (legacy 0.2.x wire); the codec
# enum for AudioFrame is resolved on demand via WavData.audio_codec.
# Bit-depth requirements per WAV spec:
#   PCM        → 16 (we don't support 8-bit unsigned PCM — that's a
#                    separate wire format we can't ship raw to the model)
#   IEEE-float → 32
#   μ-law/A-law → 8 (always, per G.711)
_FORMAT_TABLE: dict[tuple[int, int], tuple[str, int]] = {
    (_WAVE_FORMAT_PCM,        16): ("S16LE", 2),
    (_WAVE_FORMAT_IEEE_FLOAT, 32): ("F32LE", 4),
    (_WAVE_FORMAT_MULAW,       8): ("PCMU",  1),
    (_WAVE_FORMAT_ALAW,        8): ("PCMA",  1),
}


@dataclass(frozen=True)
class WavData:
    """A WAV file's data chunk + the metadata the gRPC audio message needs.

    `wire_format` is the value that goes straight into `AudioBuffer.format`
    — "S16LE" / "F32LE" / "PCMU" / "PCMA" — matching the deepfake-service
    decoder's vocabulary for the legacy Twilio-vendored AudioBuffer wire.

    `audio_codec` is the equivalent AudioCodec enum value for building
    `aurigin.media.v1.AudioFrame` messages (the new-in-0.3.0 wire shape).
    """
    samples: bytes
    rate: int
    channels: int
    wire_format: str  # "S16LE" | "F32LE" | "PCMU" | "PCMA"
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
            "F32LE": af_pb.AUDIO_CODEC_F32LE,
            "PCMU":  af_pb.AUDIO_CODEC_PCMU,
            "PCMA":  af_pb.AUDIO_CODEC_PCMA,
        }[self.wire_format]


def read_wav(path: Path) -> WavData:
    """Parse a RIFF/WAVE file into a WavData.

    Raises ValueError on:
      - non-RIFF/WAVE files (mislabeled .wav, e.g. an MP3)
      - missing data chunk
      - unsupported (format tag, bit depth) combos
    """
    buf = path.read_bytes()
    if buf[:4] != b"RIFF" or buf[8:12] != b"WAVE":
        raise ValueError(f"{path.name}: not a RIFF/WAVE file")

    offset = 12
    audio_format = channels = bits_per_sample = 0
    rate = 0
    data_start = -1
    data_len = 0
    while offset + 8 <= len(buf):
        chunk_id = buf[offset : offset + 4]
        size = int.from_bytes(buf[offset + 4 : offset + 8], "little")
        if chunk_id == b"fmt ":
            audio_format = int.from_bytes(buf[offset + 8 : offset + 10], "little")
            channels = int.from_bytes(buf[offset + 10 : offset + 12], "little")
            rate = int.from_bytes(buf[offset + 12 : offset + 16], "little")
            bits_per_sample = int.from_bytes(buf[offset + 22 : offset + 24], "little")
        elif chunk_id == b"data":
            data_start = offset + 8
            data_len = size
            break
        offset += 8 + size + (size & 1)  # chunks are word-aligned

    if data_start < 0:
        raise ValueError(f"{path.name}: no data chunk")

    key = (audio_format, bits_per_sample)
    if key not in _FORMAT_TABLE:
        raise ValueError(
            f"{path.name}: unsupported WAV (format tag 0x{audio_format:04x}, "
            f"{bits_per_sample}-bit) — expected one of: "
            f"16-bit PCM / 32-bit IEEE float / 8-bit μ-law / 8-bit A-law",
        )
    wire_format, bytes_per_frame_sample = _FORMAT_TABLE[key]
    return WavData(
        samples=buf[data_start : data_start + data_len],
        rate=rate, channels=channels, wire_format=wire_format,
        _bytes_per_frame_sample=bytes_per_frame_sample,
    )
