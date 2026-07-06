"""Yoto player panel. Stub: low priority, no backend yet."""

from __future__ import annotations

from ..ui import Line, Seg
from .base import System


class YotoSystem(System):
    name = "Yoto"
    color_key = "yoto"
    collapsed_height = 1

    def collapsed_lines(self, width: int) -> list[Line]:
        return [[Seg("not yet implemented", dim=True)]]
