"""CSV export for per-session analysis results.

Keeps the client simple — instead of letting the client know about file
handles, column ordering, escaping, and global_confidence math, the
client just collects chunk rows during the session and hands them to
`ResultCSV.write_session(...)` once `FinalResult` arrives.

Column order is the single source of truth in this file and is kept in
sync with examples/typescript/result_csv.ts so the two implementations
produce diff-friendly output for the same run.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


# Column order — keep in sync with examples/typescript/result_csv.ts.
# `chunk_score` is the raw spoof probability from AnalysisResult.score
# (= backend-app's fake_probabilities). `chunk_confidence` is the derived
# |score - 0.5| * 2 distance-from-boundary value. Keep both so cross-pipeline
# comparisons against backend-app's prob_positive are 1:1 on chunk_score.
COLUMNS: tuple[str, ...] = (
    "file_name", "prediction_id",
    "chunk_id", "chunk_offset",
    "chunk_score", "chunk_confidence", "chunk_result", "chunk_duration",
    "audio_duration", "chunks_count", "processing_time_ms",
    "global_score", "global_confidence", "global_result", "created_at",
)


@dataclass(frozen=True)
class ChunkRow:
    """One AnalysisResult flattened to its CSV-relevant fields."""
    offset_ms: int
    duration_ms: int
    score: float
    confidence: float
    label: str


class ResultCSV:
    """Per-session CSV writer. Owns the file handle; one instance per run.

    Usage:
        with ResultCSV(path) as csv_out:
            ... # client streams happen here
            csv_out.write_session(file_name, session_id, chunks,
                                  audio_duration_ms, global_result)
    """

    def __init__(self, path: str | Path) -> None:
        self._file = open(path, "w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._file)
        self._writer.writerow(COLUMNS)

    def write_session(
        self,
        file_name: str,
        session_id: str,
        chunks: list[ChunkRow],
        audio_duration_ms: int,
        global_result: str,
        processing_time_ms: float,
    ) -> None:
        """Append one row per chunk for a finished session.

        No-op when session_id is empty (e.g. the server aborted before
        emitting CreateSessionResponse).

        processing_time_ms is the wallclock from session-open (right
        before the first request is sent) to FinalResult-received. Useful
        for cross-run / cross-model throughput comparisons; differs from
        `audio_duration` which is the duration of the audio itself.
        """
        if not session_id:
            return
        # Global confidence = mean of per-chunk confidences. Matches
        # backend-app HTTP /predict's avg_confidence semantics
        # (services/prediction_service.py).
        chunks_count = len(chunks)
        global_confidence = (
            sum(c.confidence for c in chunks) / chunks_count if chunks_count else 0.0
        )
        global_score = (
            sum(c.score for c in chunks) / chunks_count if chunks_count else 0.0
        )
        created_at = datetime.now(timezone.utc).isoformat()
        for chunk_id, c in enumerate(chunks):
            self._writer.writerow([
                file_name, session_id,
                chunk_id, c.offset_ms,
                f"{c.score:.6f}", f"{c.confidence:.6f}", c.label, c.duration_ms,
                audio_duration_ms, chunks_count, f"{processing_time_ms:.1f}",
                f"{global_score:.6f}", f"{global_confidence:.6f}", global_result, created_at,
            ])

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> "ResultCSV":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
