"""Ahoos Model Studio — the runtime for the Nimbus model family.

    from studio import Registry, Orchestrator

    registry = Registry.load()
    run = Orchestrator(registry).run("build me a landing page")
    print(run.answer)
"""

from .chips import Chip, ChipError, ChipSet, import_chip, remove_chip
from .contract import Delegation, DelegationResult, Plan, Run
from .gears import Gear, GearInvocation, GearSet, ParsedRequest
from .model import NimbusModel
from .orchestrator import Orchestrator
from .registry import Registry, RegistryError
from .spec import BaseSpec, ModelSpec

__version__ = "0.1.0"

__all__ = [
    "BaseSpec",
    "Chip",
    "ChipError",
    "ChipSet",
    "Delegation",
    "DelegationResult",
    "Gear",
    "GearInvocation",
    "GearSet",
    "ModelSpec",
    "NimbusModel",
    "Orchestrator",
    "ParsedRequest",
    "Plan",
    "Registry",
    "RegistryError",
    "Run",
    "__version__",
    "import_chip",
    "remove_chip",
]
