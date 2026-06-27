// CSV export for per-session analysis results.
//
// Keeps the client simple — instead of letting the client know about file
// handles, column ordering, escaping, and global_confidence math, the
// client just collects chunk rows during the session and hands them to
// `ResultCSV.writeSession(...)` once `FinalResult` arrives.
//
// Column order is the single source of truth in this file and is kept in
// sync with examples/python/result_csv.py so the two implementations
// produce diff-friendly output for the same run.

import * as fs from "node:fs";

// Column order — keep in sync with examples/python/result_csv.py.
export const CSV_COLUMNS = [
  "file_name", "prediction_id",
  "chunk_id", "chunk_offset", "chunk_confidence", "chunk_result", "chunk_duration",
  "audio_duration", "chunks_count", "processing_time_ms",
  "global_confidence", "global_result", "created_at",
] as const;

// One AnalysisResult flattened to its CSV-relevant fields.
export interface ChunkRow {
  offsetMs: number;
  durationMs: number;
  confidence: number;
  label: string;
}

function csvEscape(value: string | number): string {
  const s = String(value);
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

// Per-session CSV writer. Owns the file handle; one instance per run.
//
// Usage:
//   const csv = new ResultCSV(path);
//   // ... streaming happens ...
//   csv.writeSession(fileName, sessionId, chunks, audioDurationMs, globalResult);
//   await csv.close();
export class ResultCSV {
  private stream: fs.WriteStream;

  constructor(path: string) {
    this.stream = fs.createWriteStream(path, { encoding: "utf-8" });
    this.stream.write(CSV_COLUMNS.join(",") + "\n");
  }

  // Append one row per chunk for a finished session. No-op when sessionId
  // is empty (e.g. the server aborted before emitting CreateSessionResponse).
  //
  // processingTimeMs is the wallclock from session-open (right before the
  // first request is sent) to FinalResult-received. Useful for cross-run /
  // cross-model throughput comparisons; differs from `audioDurationMs`
  // which is the duration of the audio itself.
  writeSession(
    fileName: string,
    sessionId: string,
    chunks: ChunkRow[],
    audioDurationMs: number,
    globalResult: string,
    processingTimeMs: number,
  ): void {
    if (!sessionId) return;
    // Global confidence = mean of per-chunk confidences. Matches
    // backend-app HTTP /predict's avg_confidence semantics
    // (services/prediction_service.py).
    const chunksCount = chunks.length;
    const globalConfidence = chunksCount
      ? chunks.reduce((acc, c) => acc + c.confidence, 0) / chunksCount
      : 0;
    const createdAt = new Date().toISOString();
    for (let chunkId = 0; chunkId < chunks.length; chunkId++) {
      const c = chunks[chunkId];
      const row = [
        fileName, sessionId,
        chunkId, c.offsetMs, c.confidence.toFixed(6), c.label, c.durationMs,
        audioDurationMs, chunksCount, processingTimeMs.toFixed(1),
        globalConfidence.toFixed(6), globalResult, createdAt,
      ].map(csvEscape).join(",");
      this.stream.write(row + "\n");
    }
  }

  close(): Promise<void> {
    return new Promise((resolve) => this.stream.end(() => resolve()));
  }
}
