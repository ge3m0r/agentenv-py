"""AgentENV's core lifecycle, expressed as a small Python package."""

import sys

if sys.version_info >= (3, 10):
    from .models import SandboxState
    from .orchestrator import Orchestrator

    __all__ = ["Orchestrator", "SandboxState"]
else:
    __all__ = []

__version__ = "0.1.0"
