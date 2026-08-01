// Tiny RIFF reader supporting linear PCM + IEEE-float + G.711 μ-law/A-law WAVs.
//
// Used by client.ts + phone_call.ts + phone_call_burst.ts — the WAV reader
// is the one piece of "I/O glue" all three examples share. In a real
// FreeSWITCH / Twilio Media Stream / SIPREC integration, this file is
// where you'd swap to your own socket / fork reader; the rest of the
// examples stay the same.
//
// Mirrors examples/python/common/wav_reader.py — same WavData shape,
// same validation, same error messages.

import * as fs from "node:fs";
import { AudioCodec } from "@aurigin/protos/aurigin/media/v1/audio_frame";

// WAVE format tags per Microsoft's RIFF spec. Anything else (ADPCM, WMA, …)
// throws — the deepfake-service decoder only accepts the codecs listed
// below.
const WAVE_FORMAT_PCM        = 0x0001;
const WAVE_FORMAT_IEEE_FLOAT = 0x0003;
const WAVE_FORMAT_ALAW       = 0x0006;
const WAVE_FORMAT_MULAW      = 0x0007;

export type WireFormat = "S16LE" | "F32LE" | "PCMU" | "PCMA";

// Format-tag + bit-depth → (wire format string, bytes-per-sample) dispatch.
// Key is `${tag}:${bits}` for string-map lookup. Bit-depth requirements
// per WAV spec:
//   PCM        → 16 (8-bit unsigned PCM is not a wire format we support)
//   IEEE-float → 32
//   μ-law/A-law → 8 (always, per G.711)
const FORMAT_TABLE: Record<string, { wireFormat: WireFormat; bytesPerSample: number }> = {
  [`${WAVE_FORMAT_PCM}:16`]:        { wireFormat: "S16LE", bytesPerSample: 2 },
  [`${WAVE_FORMAT_IEEE_FLOAT}:32`]: { wireFormat: "F32LE", bytesPerSample: 4 },
  [`${WAVE_FORMAT_MULAW}:8`]:       { wireFormat: "PCMU",  bytesPerSample: 1 },
  [`${WAVE_FORMAT_ALAW}:8`]:        { wireFormat: "PCMA",  bytesPerSample: 1 },
};

const CODEC_FOR_WIRE_FORMAT: Record<WireFormat, AudioCodec> = {
  S16LE: AudioCodec.AUDIO_CODEC_S16LE,
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
  let dataStart = -1;
  let dataLen = 0;
  while (offset + 8 <= buf.length) {
    const id = buf.toString("ascii", offset, offset + 4);
    const size = buf.readUInt32LE(offset + 4);
    if (id === "fmt ") {
      audioFormat = buf.readUInt16LE(offset + 8);
      channels = buf.readUInt16LE(offset + 10);
      sampleRate = buf.readUInt32LE(offset + 12);
      bitsPerSample = buf.readUInt16LE(offset + 22);
    } else if (id === "data") {
      dataStart = offset + 8;
      dataLen = size;
      break;
    }
    offset += 8 + size + (size & 1);  // chunks are word-aligned
  }
  if (dataStart < 0) throw new Error(`${filePath}: no data chunk`);

  const entry = FORMAT_TABLE[`${audioFormat}:${bitsPerSample}`];
  if (!entry) {
    throw new Error(
      `${filePath}: unsupported WAV (format tag 0x${audioFormat.toString(16).padStart(4, "0")}, ` +
        `${bitsPerSample}-bit) — expected one of: ` +
        `16-bit PCM / 32-bit IEEE float / 8-bit μ-law / 8-bit A-law`,
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
