"""Voice control: push-to-talk → speech-to-text → Claude tool-calling → action.

Three pieces, each swappable:

  * Recognizer (``recognizer.py``) — speech-to-text. Ships a typed-text stub so
    the whole pipeline is exercisable offline; a real mic engine drops in behind
    the same interface.
  * CommandRouter (``router.py``) — the NLU layer. Exposes each System's
    ``voice_actions()`` as Claude tools, sends the transcript to the Anthropic
    API, and dispatches the returned tool calls. No rules grammar.
  * VoiceController (``controller.py``) — the state machine + worker threads the
    shell drives: input → thinking → result.
"""

from __future__ import annotations

from .controller import VoiceController
from .recognizer import Recognizer, TextStubRecognizer
from .router import CommandRouter

__all__ = ["VoiceController", "CommandRouter", "Recognizer", "TextStubRecognizer"]
