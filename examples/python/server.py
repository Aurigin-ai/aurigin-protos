"""Minimal gRPC server using the generated aurigin-protos package.

Implements DeepfakeDetection.DetectDeepfake (bidi streaming) with stub
analysis logic so the example is runnable without an ML model. A real
service would replace _analyze() with actual inference.

Run after `pip install aurigin-protos` (or with the local generated code on PYTHONPATH).
"""

from concurrent import futures

import grpc

from aurigin.deepfake_detection.v1 import deepfake_detection_pb2 as pb
from aurigin.deepfake_detection.v1 import deepfake_detection_pb2_grpc as pb_grpc


def _bytes_to_ms(audio_bytes: int, *, channels: int, rate: int) -> int:
    """S16LE PCM: 2 bytes per sample per channel."""
    if rate <= 0 or channels <= 0:
        return 0
    return int(audio_bytes / 2 / channels / rate * 1000)


def _analyze(window_ms: int) -> tuple[float, str, float]:
    """Stub analysis: returns a fixed bonafide score. Replace with ML inference."""
    return 0.05, "bonafide", 1.0


class DeepfakeDetectionImpl(pb_grpc.DeepfakeDetectionServicer):
    def DetectDeepfake(self, request_iterator, context):  # noqa: N802 — gRPC RPC name
        # 1. First message must be CreateSessionRequest.
        first = next(request_iterator, None)
        if first is None or not first.HasField("create_session_request"):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Expected CreateSessionRequest first")
            return
        session_id = "demo-session-0001"
        yield pb.DetectDeepfakeResponse(
            create_session_response=pb.CreateSessionResponse(session_id=session_id),
        )

        # 2. Stream audio buffers, emit one AnalysisResult per buffer in this stub.
        total_ms = 0
        count = 0
        scores: list[float] = []
        for msg in request_iterator:
            if not msg.HasField("audio"):
                continue
            buf = msg.audio
            window_ms = _bytes_to_ms(len(buf.buffer), channels=buf.channels or 1, rate=buf.rate or 16000)
            score, label, confidence = _analyze(window_ms)
            scores.append(score)
            count += 1
            total_ms += window_ms
            yield pb.DetectDeepfakeResponse(
                analysis_result=pb.AnalysisResult(
                    audio_offset_ms=int(buf.pts_ns // 1_000_000),
                    duration_ms=window_ms,
                    score=score,
                    label=label,
                    confidence=confidence,
                ),
            )

        # 3. Final aggregate when client closes the stream.
        overall = sum(scores) / len(scores) if scores else 0.0
        yield pb.DetectDeepfakeResponse(
            final_result=pb.FinalResult(
                total_audio_ms=total_ms,
                overall_score=overall,
                overall_label="bonafide" if overall < 0.4 else "spoofed",
                analysis_count=count,
            ),
        )


def serve(port: int = 50051) -> None:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    pb_grpc.add_DeepfakeDetectionServicer_to_server(DeepfakeDetectionImpl(), server)
    server.add_insecure_port(f"[::]:{port}")
    server.start()
    print(f"DeepfakeDetection server listening on :{port}")
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
