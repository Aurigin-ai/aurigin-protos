// Tiny RIFF reader supporting S16LE PCM + F32LE IEEE-float WAVs.
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

// WAVE format tags. Anything else (μ-law, A-law, ADPCM, …) throws — the
// deepfake-service decoder only accepts S16LE / F32LE today.
const WAVE_FORMAT_PCM = 0x0001;
const WAVE_FORMAT_IEEE_FLOAT = 0x0003;

export type WireFormat = "S16LE" | "F32LE";

// A WAV file's data chunk + the metadata the gRPC AudioBuffer needs.
// `wireFormat` is the value that goes straight into `AudioBuffer.format`
// — matching the deepfake-service decoder's vocabulary.
export interface WavData {
  samples: Buffer;
  rate: number;
  channels: number;
  wireFormat: WireFormat;
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

  let wireFormat: WireFormat;
  if (audioFormat === WAVE_FORMAT_PCM && bitsPerSample === 16) {
    wireFormat = "S16LE";
  } else if (audioFormat === WAVE_FORMAT_IEEE_FLOAT && bitsPerSample === 32) {
    wireFormat = "F32LE";
  } else {
    throw new Error(
      `${filePath}: unsupported WAV (format tag ${audioFormat}, ${bitsPerSample}-bit) — ` +
        `expected 16-bit PCM or 32-bit IEEE float`,
    );
  }
  return {
    samples: buf.subarray(dataStart, dataStart + dataLen),
    rate: sampleRate,
    channels,
    wireFormat,
    bytesPerSample: bitsPerSample / 8,
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
