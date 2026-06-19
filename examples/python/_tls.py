"""Shared TLS auto-detect for the example clients.

Both `client.py` and `phone_call.py` call `make_channel(target)` to open a
gRPC channel that picks up the example self-signed cert at
`examples/certs/server.crt` automatically. Override via the `TLS_CA` env var
(point it at any PEM file). Set it to a path that doesn't exist to force
plain-text.
"""

from __future__ import annotations

import os
from pathlib import Path

import grpc

_DEFAULT_TLS_DIR = Path(__file__).resolve().parent.parent / "certs"


def _load_ca() -> bytes | None:
    ca_path = Path(os.environ.get("TLS_CA", _DEFAULT_TLS_DIR / "server.crt"))
    if ca_path.is_file():
        return ca_path.read_bytes()
    return None


def _load_client_keypair() -> tuple[bytes, bytes] | None:
    """Return (key_bytes, cert_bytes) for the client cert when MTLS=1, else None.

    Defaults to examples/certs/client.{crt,key}. Override via TLS_CLIENT_CERT /
    TLS_CLIENT_KEY. If MTLS=1 but either file is missing, returns None — the
    caller falls back to plain TLS and prints a notice via transport_label().
    """
    if os.environ.get("MTLS", "").lower() not in ("1", "true", "yes"):
        return None
    cert_path = Path(os.environ.get("TLS_CLIENT_CERT", _DEFAULT_TLS_DIR / "client.crt"))
    key_path = Path(os.environ.get("TLS_CLIENT_KEY", _DEFAULT_TLS_DIR / "client.key"))
    if cert_path.is_file() and key_path.is_file():
        return key_path.read_bytes(), cert_path.read_bytes()
    return None


def _channel_credentials() -> grpc.ChannelCredentials | None:
    ca = _load_ca()
    if ca is None:
        return None
    client = _load_client_keypair()
    if client is not None:
        key, cert = client
        return grpc.ssl_channel_credentials(
            root_certificates=ca, private_key=key, certificate_chain=cert,
        )
    return grpc.ssl_channel_credentials(root_certificates=ca)


def make_aio_channel(target: str) -> grpc.aio.Channel:
    """Async channel (grpc.aio)."""
    creds = _channel_credentials()
    if creds is not None:
        return grpc.aio.secure_channel(target, creds)
    return grpc.aio.insecure_channel(target)


def make_sync_channel(target: str) -> grpc.Channel:
    """Sync channel (grpc.Channel) — for `client.py`'s blocking stub."""
    creds = _channel_credentials()
    if creds is not None:
        return grpc.secure_channel(target, creds)
    return grpc.insecure_channel(target)


def transport_label() -> str:
    """Short string for the start-of-run header line."""
    if _load_ca() is None:
        return "insecure (no examples/certs/server.crt found)"
    if _load_client_keypair() is not None:
        return "mTLS (self-signed, examples/certs/)"
    if os.environ.get("MTLS", "").lower() in ("1", "true", "yes"):
        return "TLS (self-signed, examples/certs/) — MTLS=1 but client.{crt,key} missing, falling back"
    return "TLS (self-signed, examples/certs/)"
