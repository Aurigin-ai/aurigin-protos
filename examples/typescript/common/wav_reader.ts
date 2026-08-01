// Tiny RIFF reader for the WAV codecs the deepfake service consumes.
//
// Six codecs supported today; wave format tag + bits_per_sample → wire codec:
//
//   - `S16LE` — 16-bit signed linear PCM,      LE (WAVE_FORMAT_PCM 0x0001, 16-bit)
//   - `S24LE` — 24-bit signed linear PCM,      LE (WAVE_FORMAT_PCM 0x0001, 24-bit)
//   - `S32LE` — 32-bit signed linear PCM,      LE (WAVE_FORMAT_PCM 0x0001, 32-bit)
//   - `F32LE` — 32-bit IEEE-float PCM,         LE (WAVE_FORMAT_IEEE_FLOAT 0x0003)
//   - `PCMU`  — G.711 μ-law,  8-bit               (WAVE_FORMAT_MULAW 0x0007)
//   - `PCMA`  — G.711 A-law,  8-bit               (WAVE_FORMAT_ALAW  0x0006)
//
// Envelope-transparent:
//   - WAVE_FORMAT_EXTENSIBLE (0xFFFE) — the extension chunk carries a
//     SubFormat GUID whose first 4 bytes are the "real" format tag. We
//     verify the GUID suffix matches Microsoft's KSDATAFORMAT_SUBTYPE_*
//     pattern, then dispatch as if the file used that tag directly.
//
// Design note: all six codecs pass through as raw wire bytes. Client-side
// conversion (e.g. int24 → float32) is deliberately avoided — the deepfake
// service decodes every codec natively via vectorised numpy, so every
// ingress (Python client, TypeScript client, aurigin-router, future SDKs)
// gets the same fast path for free.
//
// Mirrors examples/python/common/wav_reader.py — same WavData shape,
// same validation, same error messages.

import * as fs from "node:fs";
import { AudioCodec } from "@aurigin/protos/aurigin/media/v1/audio_frame";

// WAVE format tags per Microsoft's RIFF spec.
const WAVE_FORMAT_PCM        = 0x0001;
const WAVE_FORMAT_IEEE_FLOAT = 0x0003;
const WAVE_FORMAT_ALAW       = 0x0006;
const WAVE_FORMAT_MULAW      = 0x0007;
const WAVE_FORMAT_EXTENSIBLE = 0xFFFE;

// KSDATAFORMAT_SUBTYPE_* GUID suffix. Microsoft's standard EXTENSIBLE
// sub-format GUIDs follow the pattern (canonical GUID string form):
//   xxxxxxxx-0000-0010-8000-00aa00389b71
// where xxxxxxxx is the format tag as an 8-hex-digit uint32.
//
// On the wire, GUIDs use Microsoft's mixed-endianness layout:
//   - first 4 bytes:  little-endian uint32   ← format tag
//   - next 2 bytes:   little-endian uint16   ← "0000"
//   - next 2 bytes:   little-endian uint16   ← "0010" → 10 00 on the wire
//   - last 8 bytes:   big-endian (raw)       ← 80 00 00 aa 00 38 9b 71
const KSDATAFORMAT_SUBTYPE_SUFFIX = Buffer.from("00001000800000aa00389b71", "hex");

export type WireFormat = "S16LE" | "S24LE" | "S32LE" | "F32LE" | "PCMU" | "PCMA";

// Format-tag + bit-depth → (wire format string, bytes-per-sample) dispatch.
// Key is `${tag}:${bits}` for string-map lookup.
const FORMAT_TABLE: Record<string, { wireFormat: WireFormat; bytesPerSample: number }> = {
  [`${WAVE_FORMAT_PCM}:16`]:        { wireFormat: "S16LE", bytesPerSample: 2 },
  [`${WAVE_FORMAT_PCM}:24`]:        { wireFormat: "S24LE", bytesPerSample: 3 },
  [`${WAVE_FORMAT_PCM}:32`]:        { wireFormat: "S32LE", bytesPerSample: 4 },
  [`${WAVE_FORMAT_IEEE_FLOAT}:32`]: { wireFormat: "F32LE", bytesPerSample: 4 },
  [`${WAVE_FORMAT_MULAW}:8`]:       { wireFormat: "PCMU",  bytesPerSample: 1 },
  [`${WAVE_FORMAT_ALAW}:8`]:        { wireFormat: "PCMA",  bytesPerSample: 1 },
};

const CODEC_FOR_WIRE_FORMAT: Record<WireFormat, AudioCodec> = {
  S16LE: AudioCodec.AUDIO_CODEC_S16LE,
  S24LE: AudioCodec.AUDIO_CODEC_S24LE,
  S32LE: AudioCodec.AUDIO_CODEC_S32LE,
  F32LE: AudioCodec.AUDIO_CODEC_F32LE,
  PCMU:  AudioCodec.AUDIO_CODEC_PCMU,
  PCMA:  AudioCodec.AUDIO_CODEC_PCMA,
};

// A WAV file's data chunk + the metadata the gRPC audio message needs.
// `wireFormat` is the value that goes straight into `AudioBuffer.format`
// (legacy 0.2.x wire); `audioCodec` is the equivalent AudioCodec enum
// for building AudioFrame (0.3.0+ wire).
export interface WavData {
  samples: Buffer;
  rate: number;
  channels: number;
  wireFormat: WireFormat;
  audioCodec: AudioCodec;
  bytesPerSample: number;  // per *sample*, not per frame — multiply by channels for frame size
}

// Extract the real format tag from a WAVE_FORMAT_EXTENSIBLE fmt chunk.
// Throws if the SubFormat GUID isn't a Microsoft KSDATAFORMAT_SUBTYPE_*.
function resolveExtensible(fmtBody: Buffer): number {
  if (fmtBody.length < 40) {
    throw new Error(`EXTENSIBLE fmt chunk too short (got ${fmtBody.length} bytes, need ≥ 40)`);
  }
  const guid = fmtBody.subarray(24, 40);
  if (!guid.subarray(4, 16).equals(KSDATAFORMAT_SUBTYPE_SUFFIX)) {
    throw new Error(
      `EXTENSIBLE SubFormat GUID ${guid.toString("hex")} is not a ` +
        `KSDATAFORMAT_SUBTYPE_* — unsupported vendor sub-format`,
    );
  }
  return guid.readUInt32LE(0);
}

export function readWav(filePath: string): WavData {
  const buf = fs.readFileSync(filePath);
  if (buf.toString("ascii", 0, 4) !== "RIFF" || buf.toString("ascii", 8, 12) !== "WAVE") {
    throw new Error(`${filePath}: not a RIFF/WAVE file`);
  }

  // Walk RIFF chunks to find fmt + data (handles non-canonical orderings).
  let offset = 12;
  let audioFormat = 0;
  let sampleRate = 0;
  let channels = 0;
  let bitsPerSample = 0;
  let fmtBody = Buffer.alloc(0);
  let dataStart = -1;
  let dataLen = 0;
  while (offset + 8 <= buf.length) {
    const id = buf.toString("ascii", offset, offset + 4);
    const size = buf.readUInt32LE(offset + 4);
    if (id === "fmt ") {
      fmtBody = buf.subarray(offset + 8, offset + 8 + size);
      audioFormat = fmtBody.readUInt16LE(0);
      channels = fmtBody.readUInt16LE(2);
      sampleRate = fmtBody.readUInt32LE(4);
      bitsPerSample = fmtBody.readUInt16LE(14);
    } else if (id === "data") {
      dataStart = offset + 8;
      dataLen = size;
      break;
    }
    offset += 8 + size + (size & 1);  // chunks are word-aligned
  }
  if (dataStart < 0) throw new Error(`${filePath}: no data chunk`);

  // EXTENSIBLE: unwrap the SubFormat GUID and keep going with the real tag.
  // bits_per_sample and channels stay the same — EXTENSIBLE only changes
  // the codec identifier, not the sample width or layout.
  if (audioFormat === WAVE_FORMAT_EXTENSIBLE) {
    try {
      audioFormat = resolveExtensible(fmtBody);
    } catch (e) {
      throw new Error(`${filePath}: ${(e as Error).message}`);
    }
  }

  const entry = FORMAT_TABLE[`${audioFormat}:${bitsPerSample}`];
  if (!entry) {
    throw new Error(
      `${filePath}: unsupported WAV (format tag 0x${audioFormat.toString(16).padStart(4, "0")}, ` +
        `${bitsPerSample}-bit) — expected one of: ` +
        `16/24/32-bit PCM / 32-bit IEEE float / 8-bit μ-law / 8-bit A-law`,
    );
  }
  return {
    samples: buf.subarray(dataStart, dataStart + dataLen),
    rate: sampleRate,
    channels,
    wireFormat: entry.wireFormat,
    audioCodec: CODEC_FOR_WIRE_FORMAT[entry.wireFormat],
    bytesPerSample: entry.bytesPerSample,
  };
}

// Computed convenience getters (kept as standalone helpers — TS interfaces
// don't have getters and we don't want to switch to a class for two values).
export function bytesPerFrame(wav: WavData): number {
  return wav.bytesPerSample * wav.channels;
}

export function durationS(wav: WavData): number {
  const denom = wav.rate * bytesPerFrame(wav);
  return denom ? wav.samples.length / denom : 0;
}
