"""Scenario-driven gRPC simulator for the aurigin DeepfakeDetection service.

Ships as an installable Python package + Docker image so downstream teams can
smoke-test their aurigin-protos client integrations without needing the real
deepfake-service. See README.md for usage.
"""

from .server import serve

__all__ = ["serve"]
