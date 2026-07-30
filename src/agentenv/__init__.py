"""AgentENV's core lifecycle, expressed as a small Python package."""

import sys

if sys.version_info >= (3, 10):
    from .backend import DockerSandboxBackend, LocalProcessBackend
    from .models import NetworkPolicy, ResourceLimits, SandboxState
    from .oci import OCIReference
    from .orchestrator import Orchestrator

    __all__ = [
        "DockerSandboxBackend",
        "LocalProcessBackend",
        "NetworkPolicy",
        "OCIReference",
        "Orchestrator",
        "ResourceLimits",
        "SandboxState",
    ]
else:
    __all__ = []

__version__ = "0.2.0"
