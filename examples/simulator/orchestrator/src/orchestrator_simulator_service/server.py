"""Minimal grpc.aio server hosting aurigin.client.v1.AudioVerification.

Design goals — as small as it can be while still round-tripping a real
gRPC session end-to-end + demonstrating the mandatory auth handshake:

- Terminates `Stream` bidi.
- Session lifecycle: auth check -> handshake -> periodic Verdict ->
  FinalResult on close.
- Auth is MANDATORY. Every Stream call must carry `authorization:
  Bearer <token>` in gRPC metadata (matches the platform spec — same
  header for JWTs and API keys). Missing / malformed / unrecognised
  tokens abort with UNAUTHENTICATED before any application state is
  allocated.
- Two accepted tokens by default — one JWT-shaped and one API-key-shaped
  — so SDK developers can smoke-test both `CALLER_TYPE_USER` and
  `CALLER_TYPE_CLIENT` code paths against the same simulator. Override
  via the ORCHESTRATOR_SIM_JWT / ORCHESTRATOR_SIM_API_KEY env vars.
- No scenarios, no downstream dispatch, no config — a canned response
  loop. `Verify` returns UNIMPLEMENTED (SDK smoke tests use Stream +
  half-close).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import uuid

import grpc
from aurigin.client.v1 import audio_verification_pb2 as pb
from aurigin.client.v1 import audio_verification_pb2_grpc as pb_grpc
from aurigin.common.v1 import result_pb2 as result_pb

log = logging.getLogger("orchestrator_simulator_service")

# Canned verdict cadence. Matches the deepfake service's default 5 s
# window so the wire shape a client sees here is representative.
_VERDICT_INTERVAL_S = 5.0
_CANNED_CONSUMER_NAME = "deepfake"
_CANNED_WINDOW_MS = int(_VERDICT_INTERVAL_S * 1000)

# Demo token defaults. NOT real credentials — the simulator does exact
# string matching, not JWT signature verification / API-key DB lookup.
# Real orchestrator delegates both to auth-service. Overridden per
# deployment via env vars below.
_DEFAULT_DEMO_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiJkZW1vLXVzZXIiLCJ0ZW5hbnQiOiJkZW1vLXRlbmFudCIsImlhdCI6MTcwMDAwMDAwMH0."
    "aurigin-sim-demo-signature-not-verified"
)
_DEFAULT_DEMO_API_KEY = "sk_test_aurigin_sim_demo_0000000000000000"


def _mint_session_id() -> str:
    return f"call-{uuid.uuid4().hex}"


class AudioVerificationSimulatorServicer(pb_grpc.AudioVerificationServicer):
    def __init__(self, accepted_tokens: set[str], *, auth_enabled: bool = True) -> None:
        if auth_enabled and not accepted_tokens:
            raise ValueError(
                "AudioVerificationSimulatorServicer with auth_enabled=True requires at "
                "least one accepted token. Set AUTH_ENABLED=false to bypass auth (dev only).",
            )
        self._accepted = accepted_tokens
        self._auth_enabled = auth_enabled

    def _check_auth(self, context: grpc.aio.ServicerContext) -> str | None:
        """Return the presented token if accepted, else None (caller aborts).

        When auth_enabled=False, returns a sentinel string so callers see
        a truthy value and skip the abort path — no token is inspected.
        """
        if not self._auth_enabled:
            return "(auth-disabled)"
        # invocation_metadata is a sequence of (key, value) pairs; keys are
        # lowercased by grpc-python. Take the first `authorization` header
        # if the client sent multiple (they shouldn't).
        for k, v in context.invocation_metadata():
            if k.lower() != "authorization":
                continue
            if not v.startswith("Bearer "):
                return None
            token = v[len("Bearer "):].strip()
            if token in self._accepted:
                return token
            return None
        return None

    async def Stream(  # noqa: N802 — grpc-generated method name
        self,
        request_iterator,
        context: grpc.aio.ServicerContext,
    ):
        # 0. Auth — mandatory. Reject BEFORE consuming the request stream.
        token = self._check_auth(context)
        if token is None:
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "missing or invalid 'authorization: Bearer <token>' metadata; "
                "the simulator accepts the demo JWT + demo API key by default "
                "(see ORCHESTRATOR_SIM_JWT / ORCHESTRATOR_SIM_API_KEY env vars).",
            )
            return

        # 1. Handshake — first message must be CreateSessionRequest.
        try:
            first_req = await anext(request_iterator)
        except StopAsyncIteration:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "empty request stream")
            return
        if first_req.WhichOneof("request") != "create_session_request":
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "first message must be CreateSessionRequest",
            )
            return

        session_id = _mint_session_id()
        log.info(
            "stream open: session=%s peer=%s token_prefix=%s",
            session_id,
            context.peer(),
            token[:10],
        )
        yield pb.StreamResponse(
            create_session_response=pb.CreateSessionResponse(session_id=session_id),
        )

        # 2. Drain audio + emit a canned Verdict every _VERDICT_INTERVAL_S.
        # Two concurrent tasks: one draining request_iterator so the client
        # can stream freely, one ticking on the interval. Whichever finishes
        # first (client half-closes or stream cancelled) tears down the other.
        verdict_count = 0

        async def _drain() -> None:
            async for _req in request_iterator:
                # Silently discard — the sim doesn't need the audio.
                pass

        drain_task = asyncio.create_task(_drain(), name=f"drain-{session_id}")
        try:
            while not drain_task.done():
                try:
                    await asyncio.wait_for(
                        asyncio.shield(drain_task),
                        timeout=_VERDICT_INTERVAL_S,
                    )
                except asyncio.TimeoutError:
                    # Time to emit another canned Verdict.
                    yield pb.StreamResponse(
                        verdict=pb.Verdict(
                            consumer_name=_CANNED_CONSUMER_NAME,
                            audio_offset_ms=verdict_count * _CANNED_WINDOW_MS,
                            duration_ms=_CANNED_WINDOW_MS,
                            label=result_pb.RESULT_LABEL_BONAFIDE,
                            label_raw="bonafide",
                            score=0.5,
                            confidence=0.5,
                        ),
                    )
                    verdict_count += 1
        finally:
            if not drain_task.done():
                drain_task.cancel()
                try:
                    await drain_task
                except (asyncio.CancelledError, Exception):
                    pass

        # 3. Terminal aggregate.
        yield pb.StreamResponse(
            final_result=pb.FinalResult(
                total_audio_ms=verdict_count * _CANNED_WINDOW_MS,
                per_consumer=[
                    pb.ConsumerFinalResult(
                        consumer_name=_CANNED_CONSUMER_NAME,
                        overall_label=result_pb.RESULT_LABEL_BONAFIDE,
                        overall_label_raw="bonafide",
                        overall_score=0.5,
                        analysis_count=verdict_count,
                    ),
                ],
            ),
        )
        log.info(
            "stream closed: session=%s verdicts=%d",
            session_id,
            verdict_count,
        )

    async def Verify(  # noqa: N802 — grpc-generated method name
        self,
        _request: pb.VerifyRequest,
        context: grpc.aio.ServicerContext,
    ) -> pb.VerifyResponse:
        # Auth check still applies — same UNAUTHENTICATED behaviour as Stream.
        if self._check_auth(context) is None:
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "missing or invalid 'authorization: Bearer <token>' metadata",
            )
            return pb.VerifyResponse()  # unreachable
        await context.abort(
            grpc.StatusCode.UNIMPLEMENTED,
            "Verify is not implemented in this simulator — use Stream + half-close.",
        )
        return pb.VerifyResponse()  # unreachable; satisfies type checker


def _load_accepted_tokens() -> set[str]:
    """Assemble the set of tokens the simulator will accept.

    Both env vars carry a default so `docker compose up` works with no
    configuration; unset either to skip that auth flavour, or override
    with a real value for smoke-testing against a specific token.
    """
    tokens: set[str] = set()
    jwt = os.environ.get("ORCHESTRATOR_SIM_JWT", _DEFAULT_DEMO_JWT)
    if jwt:
        tokens.add(jwt)
    api_key = os.environ.get("ORCHESTRATOR_SIM_API_KEY", _DEFAULT_DEMO_API_KEY)
    if api_key:
        tokens.add(api_key)
    return tokens


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


async def serve(port: int) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    auth_enabled = _env_bool("AUTH_ENABLED", True)
    accepted = _load_accepted_tokens() if auth_enabled else set()
    if auth_enabled:
        log.info(
            "auth ENABLED — %d accepted token(s), JWT(%s) API_KEY(%s)",
            len(accepted),
            "on" if os.environ.get("ORCHESTRATOR_SIM_JWT", _DEFAULT_DEMO_JWT) else "off",
            "on" if os.environ.get("ORCHESTRATOR_SIM_API_KEY", _DEFAULT_DEMO_API_KEY) else "off",
        )
    else:
        log.warning(
            "auth DISABLED (AUTH_ENABLED=false) — every request accepted. "
            "Dev / smoke-test only; never disable in a shared or public deployment.",
        )

    server = grpc.aio.server()
    pb_grpc.add_AudioVerificationServicer_to_server(
        AudioVerificationSimulatorServicer(accepted, auth_enabled=auth_enabled),
        server,
    )
    bind = f"0.0.0.0:{port}"
    bound = server.add_insecure_port(bind)
    log.info("orchestrator simulator listening on %s (bound port %d)", bind, bound)

    await server.start()

    # Graceful shutdown on SIGINT / SIGTERM — matches deepfake sim behaviour.
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # Signal handlers unavailable on some platforms (Windows). Fall
            # back to KeyboardInterrupt propagation via wait_for_termination.
            pass

    stop_task = asyncio.create_task(stop_event.wait(), name="stop-waiter")
    term_task = asyncio.create_task(server.wait_for_termination(), name="server-wait")
    done, pending = await asyncio.wait(
        {stop_task, term_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for t in pending:
        t.cancel()

    log.info("shutting down")
    await server.stop(grace=5)
    sys.stdout.flush()
