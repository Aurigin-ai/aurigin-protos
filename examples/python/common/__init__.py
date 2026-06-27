"""Shared helpers for the aurigin-protos example clients/server.

Three small modules — all "infra glue" rather than the actual gRPC
example, so they live here together to keep the call-site files
focused on what they're demonstrating:

  - wav_reader  — WavData dataclass + read_wav() (S16LE / F32LE)
  - result_csv  — ResultCSV writer for per-chunk run captures
  - tls         — TLS auto-detect for example clients (server-cert + mTLS)
  - shutdown    — graceful SIGINT/SIGTERM handler for asyncio task lists

Top-level re-exports so call sites can do:
    from common import WavData, read_wav, ResultCSV, install_signal_shutdown
"""
from .result_csv import ChunkRow, ResultCSV
from .shutdown import Shutdown, install_signal_shutdown
from .tls import make_aio_channel, make_sync_channel, transport_label
from .wav_reader import WavData, read_wav

__all__ = [
    "ChunkRow",
    "ResultCSV",
    "Shutdown",
    "WavData",
    "install_signal_shutdown",
    "make_aio_channel",
    "make_sync_channel",
    "read_wav",
    "transport_label",
]
