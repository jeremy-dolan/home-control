"""Background polling: one daemon thread per system.

Each thread loops calling `system.poll(focused)` at the system's focused/idle
cadence, depending on whether it's the currently focused panel. Systems write
to their own cached snapshot inside poll(); the render loop on the main thread
only reads those snapshots, so the UI never blocks on network I/O.
"""

from __future__ import annotations

import threading

from .systems.base import System


class Poller:
    def __init__(self, systems: list[System]):
        self._systems = systems
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._focused_idx = 0
        self._lock = threading.Lock()

    def set_focus(self, idx: int) -> None:
        with self._lock:
            self._focused_idx = idx

    def _focused(self, idx: int) -> bool:
        with self._lock:
            return idx == self._focused_idx

    def start(self) -> None:
        for i, system in enumerate(self._systems):
            t = threading.Thread(target=self._run, args=(i, system), daemon=True)
            t.start()
            self._threads.append(t)

    def _run(self, idx: int, system: System) -> None:
        while not self._stop.is_set():
            focused = self._focused(idx)
            try:
                system.poll(focused)
            except Exception:
                # A misbehaving system must never kill its poll thread.
                pass
            interval = system.poll_interval_focused if focused else system.poll_interval_idle
            # Wait returns early if stop is set, so shutdown is prompt.
            self._stop.wait(interval)

    def stop(self) -> None:
        self._stop.set()
