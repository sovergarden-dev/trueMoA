"""True MoA — strict-failover guard for Hermes Mixture of Agents.

Import surface: `install()`, `uninstall()`, `status()`, `MoaReferenceOutage`.
Everything else is internal to the gate.
"""

from __future__ import annotations

from .gate import MoaReferenceOutage, install, status, uninstall
from .state import STATE_PATH, load, save, set_enabled

__all__ = [
    "MoaReferenceOutage",
    "STATE_PATH",
    "install",
    "load",
    "save",
    "set_enabled",
    "status",
    "uninstall",
]
