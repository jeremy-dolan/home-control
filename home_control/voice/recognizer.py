"""Speech-to-text behind a swappable interface.

The TUI can't capture a microphone in this environment, so the shipped engine is
a typed-text stub: the shell collects a line and hands it to ``transcribe()``.
A real engine (e.g. local faster-whisper) implements the same interface with
``text_input = False`` and records from the mic inside ``transcribe()``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class Recognizer(ABC):
    # When True, the UI must collect a typed line and pass it to transcribe().
    # When False, transcribe() captures audio itself (real mic engine).
    text_input: bool = False

    @abstractmethod
    def transcribe(self, typed: str | None = None) -> str:
        """Return a transcript. For text-input recognizers, ``typed`` is the
        line the UI collected; mic engines ignore it and record instead."""
        raise NotImplementedError


class TextStubRecognizer(Recognizer):
    """Stand-in STT: the transcript is whatever the user typed."""

    text_input = True

    def transcribe(self, typed: str | None = None) -> str:
        return (typed or "").strip()
