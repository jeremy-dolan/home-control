"""System registry. `build_systems()` returns the panels in display order."""

from __future__ import annotations

from .base import System
from .hue import HueSystem
from .midea import MideaSystem
from .roku import RokuSystem
from .router import RouterSystem
from .sonos import SonosSystem
from .yoto import YotoSystem

# Display order, top to bottom. Matches the design mockup, low-priority last.
_DEFAULT_ORDER: list[type[System]] = [
    RouterSystem,
    HueSystem,
    RokuSystem,
    SonosSystem,
    YotoSystem,
    MideaSystem,
]


def build_systems() -> list[System]:
    return [cls() for cls in _DEFAULT_ORDER]


__all__ = ["System", "build_systems"]
