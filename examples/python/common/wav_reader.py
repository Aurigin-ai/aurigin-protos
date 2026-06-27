"""Tiny RIFF reader supporting S16LE PCM + F32LE IEEE-float WAVs.

The stdlib `wave` module rejects float WAVs (raises on format tag 0x0003),
so we parse RIFF ourselves and dispatch the audio_format tag to the wire
formats the deepfake-service decoder accepts: S16LE (16-bit PCM) and
F32LE (32-bit IEEE float).

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

# WAVE format tags. Anything else (μ-law, A-law, ADPCM, …) raises ValueError —
# the deepfake-service decoder only accepts S16LE / F32LE today.
_WAVE_FORMAT_PCM = 0x0001
_WAVE_FORMAT_IEEE_FLOAT = 0x0003


@dataclass(frozen=True)
class WavData:
    """A WAV file's data chunk + the metadata the gRPC AudioBuffer needs.

    `wire_format` is the value that goes straight into `AudioBuffer.format`
    — "S16LE" or "F32LE" — matching the deepfake-service decoder's vocabulary.
    """
    samples: bytes
    rate: int
    channels: int
    wire_format: str  # "S16LE" | "F32LE"

    @property
    def bytes_per_sample(self) -> int:
        """Bytes per audio frame (sample × channels). 2 for S16LE, 4 for F32LE."""
        return (4 if self.wire_format == "F32LE" else 2) * self.channels

    @property
    def duration_s(self) -> float:
        denom = self.rate * self.bytes_per_sample
        return len(self.samples) / denom if denom else 0.0


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

    if audio_format == _WAVE_FORMAT_PCM and bits_per_sample == 16:
        wire_format = "S16LE"
    elif audio_format == _WAVE_FORMAT_IEEE_FLOAT and bits_per_sample == 32:
        wire_format = "F32LE"
    else:
        raise ValueError(
            f"{path.name}: unsupported WAV (format tag {audio_format}, "
            f"{bits_per_sample}-bit) — expected 16-bit PCM or 32-bit IEEE float",
        )
    return WavData(
        samples=buf[data_start : data_start + data_len],
        rate=rate, channels=channels, wire_format=wire_format,
    )
