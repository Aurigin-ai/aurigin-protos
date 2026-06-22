"""Scenario-driven simulator runtime for the DeepfakeDetection example server.

Public entry points:
    load_scenarios(dir)  -> dict[str, Scenario]
    run_session(...)     -> async generator yielding DetectDeepfakeResponse
"""

from .loader import Scenario, load_scenarios
from .runner import run_session

__all__ = ["Scenario", "load_scenarios", "run_session"]
