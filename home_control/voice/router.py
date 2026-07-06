"""NLU via the Claude API: transcript + the command surface as tools → actions.

Each System's ``voice_actions()`` becomes a Claude tool. The transcript goes to
the Anthropic Messages API (a low-latency model); returned tool calls are
dispatched to the systems' handlers in a bounded manual loop. There is no rules
grammar — Claude maps natural phrasing onto the tools directly.

The Anthropic SDK is imported lazily so the rest of the app runs without it, and
``available()`` lets the UI degrade gracefully when no API key is configured.
"""

from __future__ import annotations

import os
from typing import Any

from .. import config
from ..systems.base import System

# Low-latency model for snappy commands (Haiku 4.5). Override via [voice] model.
DEFAULT_MODEL = "claude-haiku-4-5"

SYSTEM_PROMPT = (
    "You control a smart home through the provided tools. The user speaks a "
    "command; call the tool(s) that carry it out. Match rooms, speakers, and "
    "apps to the closest available device named below. If a command maps to no "
    "tool — including greetings, small talk, or questions about what you can "
    "do — reply in one short sentence with no lists. Keep any spoken reply to "
    "one short sentence."
)

# Bound the tool-use loop so a misbehaving turn can't spin forever.
MAX_TURNS = 5
MAX_TOKENS = 1024


class CommandRouter:
    def __init__(self, systems: list[System], *, api_key: str | None = None, model: str | None = None):
        self.systems = systems
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY") or config.get("voice", "api_key")
        self.model = model or config.get("voice", "model", DEFAULT_MODEL)
        self._client = None  # lazy Anthropic client

    def available(self) -> bool:
        """True when an API key is configured (env or [voice] in config.toml)."""
        return bool(self.api_key)

    # -- tool surface ------------------------------------------------------
    # Tools are plain dicts the SDK accepts at runtime; typed as Any so they
    # satisfy the SDK's invariant TypedDict param types without per-key casts.
    def _build(self) -> tuple[list[Any], dict[str, Any]]:
        tools: list[Any] = []
        handlers: dict[str, Any] = {}
        for system in self.systems:
            for action in system.voice_actions():
                tools.append({
                    "name": action.name,
                    "description": action.description,
                    "input_schema": {
                        "type": "object",
                        "properties": action.parameters,
                        "required": action.required,
                        "additionalProperties": False,
                    },
                })
                handlers[action.name] = action.handler
        return tools, handlers

    def _context(self) -> str:
        parts = [s.voice_context() for s in self.systems]
        return "\n".join(p for p in parts if p)

    # -- dispatch ----------------------------------------------------------
    def handle(self, transcript: str) -> str:
        """Run one command end-to-end; return a short result summary."""
        if not self.available():
            raise RuntimeError("Voice unavailable: set ANTHROPIC_API_KEY")
        transcript = transcript.strip()
        if not transcript:
            return "I didn't catch that."

        import anthropic  # lazy: app runs fine without the SDK installed

        if self._client is None:
            self._client = anthropic.Anthropic(api_key=self.api_key)

        tools, handlers = self._build()
        system = SYSTEM_PROMPT
        context = self._context()
        if context:
            system += "\n\nAvailable devices:\n" + context

        messages: list[Any] = [{"role": "user", "content": transcript}]
        results: list[str] = []

        for _ in range(MAX_TURNS):
            resp = self._client.messages.create(
                model=self.model, max_tokens=MAX_TOKENS,
                system=system, tools=tools, messages=messages,
            )
            if resp.stop_reason != "tool_use":
                if results:
                    return "; ".join(results)
                text = " ".join(b.text for b in resp.content if b.type == "text").strip()
                return text or "Done."

            messages.append({"role": "assistant", "content": resp.content})
            tool_results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                handler = handlers.get(block.name)
                try:
                    out = handler(block.input) if handler else f"Unknown action: {block.name}"
                except Exception as e:  # noqa: BLE001 — surface any handler failure to the model
                    out = f"Error: {e}"
                results.append(out)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": out})
            messages.append({"role": "user", "content": tool_results})

        return "; ".join(results) if results else "I couldn't complete that."
