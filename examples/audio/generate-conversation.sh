#!/usr/bin/env bash
# Stitch every .wav in examples/audio/ into a single 8 kHz mono S16LE
# WAV that mimics a FreeSWITCH-style narrowband phone call: each input
# chunk becomes a "turn", separated by a short silence to suggest
# turn-taking, with the whole thing resampled to G.711-compatible
# 8 kHz / mono / 16-bit PCM (linear, not companded).
#
# Output: examples/audio/conversation_8khz.wav (next to this script)
#
# Usage:
#   bash examples/audio/generate-conversation.sh [--gap-ms 500] [--repeat 1]
#
# Prerequisites: ffmpeg.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUDIO_DIR="$SCRIPT_DIR"
OUT="$AUDIO_DIR/conversation_8khz.wav"

GAP_MS=500
REPEAT=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --gap-ms) GAP_MS="$2"; shift 2 ;;
    --repeat) REPEAT="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

shopt -s nullglob
sources=( "$AUDIO_DIR"/*.wav )
# Exclude the output file if it already exists, so re-runs are stable.
filtered=()
for f in "${sources[@]}"; do
  [[ "$(basename "$f")" == "conversation_8khz.wav" ]] && continue
  filtered+=( "$f" )
done

if [[ ${#filtered[@]} -eq 0 ]]; then
  echo "No source .wav files in $AUDIO_DIR. Drop a few in and re-run." >&2
  exit 1
fi

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

# 1. Resample each source to 8 kHz mono S16LE.
i=0
declare -a resampled
for src in "${filtered[@]}"; do
  out="$TMPDIR/turn_$(printf '%02d' "$i").wav"
  ffmpeg -hide_banner -loglevel error -y -i "$src" \
    -ar 8000 -ac 1 -sample_fmt s16 -c:a pcm_s16le "$out"
  resampled+=( "$out" )
  i=$((i+1))
done

# 2. Generate a `--gap-ms` silence file at 8 kHz mono S16LE.
SILENCE="$TMPDIR/silence.wav"
SILENCE_S=$(awk -v ms="$GAP_MS" 'BEGIN { printf "%.3f", ms/1000 }')
ffmpeg -hide_banner -loglevel error -y \
  -f lavfi -i "anullsrc=r=8000:cl=mono" -t "$SILENCE_S" \
  -sample_fmt s16 -c:a pcm_s16le "$SILENCE"

# 3. Build a concat list: turn, silence, turn, silence, ... (repeated).
LIST="$TMPDIR/concat.txt"
: > "$LIST"
for ((r=0; r<REPEAT; r++)); do
  for j in "${!resampled[@]}"; do
    echo "file '${resampled[$j]}'" >> "$LIST"
    if (( j < ${#resampled[@]} - 1 || r < REPEAT - 1 )); then
      echo "file '$SILENCE'" >> "$LIST"
    fi
  done
done

# 4. Concat-encode into the final 8 kHz mono S16LE WAV.
ffmpeg -hide_banner -loglevel error -y -f concat -safe 0 -i "$LIST" \
  -ar 8000 -ac 1 -sample_fmt s16 -c:a pcm_s16le "$OUT"

# 5. Report.
DURATION=$(ffprobe -hide_banner -loglevel error -of csv=p=0 \
  -show_entries format=duration "$OUT" 2>/dev/null || echo '?')
echo "Wrote $OUT (${#filtered[@]} turns × $REPEAT, ~${DURATION}s @ 8000 Hz mono S16LE)"
