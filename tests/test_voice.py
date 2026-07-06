"""Voice-mode tests: recognizer stub, router tool-surface/dispatch, controller
state machine. No network and no curses init — the live LLM call is not exercised
here (it needs a real API key); everything around it is."""

import curses

from home_control import config
from home_control.systems.hue import HueSystem
from home_control.systems.roku import RokuSystem
from home_control.voice.controller import VoiceController
from home_control.voice.recognizer import TextStubRecognizer
from home_control.voice.router import CommandRouter

# --- Recognizer --------------------------------------------------------------


def test_text_stub_recognizer():
    r = TextStubRecognizer()
    assert r.text_input is True
    assert r.transcribe("  turn on the lights  ") == "turn on the lights"
    assert r.transcribe(None) == ""


# --- Router ------------------------------------------------------------------


def _mock_systems(monkeypatch):
    monkeypatch.setenv("HOME_CONTROL_MOCK", "1")
    hue, roku = HueSystem(), RokuSystem()
    hue.poll(True)   # load fixture rooms/lights
    roku.poll(True)  # load fixture device/apps
    return hue, roku


def test_router_builds_tool_surface(monkeypatch):
    hue, roku = _mock_systems(monkeypatch)
    router = CommandRouter([hue, roku], api_key="test-key")
    tools, handlers = router._build()
    names = {t["name"] for t in tools}
    assert {"lights_power", "lights_brightness", "lights_scene", "roku_launch", "roku_button"} <= names
    for t in tools:  # every tool is a well-formed object schema
        schema = t["input_schema"]
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert set(schema["required"]) <= set(schema["properties"])
    assert set(handlers) == names


def test_router_context_lists_devices(monkeypatch):
    hue, roku = _mock_systems(monkeypatch)
    ctx = CommandRouter([hue, roku], api_key="k")._context()
    assert "Kitchen" in ctx          # hue room
    assert "Roku apps" in ctx        # roku apps line


def test_router_unavailable_without_key(monkeypatch, tmp_path):
    # Isolate from any real ~/.config/home-control/config.toml on the machine
    # running the tests, which may have a live [voice] api_key configured.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(config, "_cache", None)
    assert CommandRouter([], api_key=None).available() is False
    assert CommandRouter([], api_key="x").available() is True


# --- Action handlers (dispatched directly, mock controllers) -----------------


def test_hue_voice_handlers(monkeypatch):
    hue, _ = _mock_systems(monkeypatch)
    _, handlers = CommandRouter([hue], api_key="k")._build()
    assert "all lights on" in handlers["lights_power"]({"on": True}).lower()
    assert handlers["lights_power"]({"room": "kitchen", "on": False}) == "Turned off kitchen"
    assert "No room" in handlers["lights_power"]({"room": "nowhere", "on": True})
    assert handlers["lights_brightness"]({"room": "kitchen", "percent": 40}) == "kitchen set to 40%"
    assert "Relax" in handlers["lights_scene"]({"room": "Living room", "scene": "Relax"})


def test_roku_voice_handlers(monkeypatch):
    _, roku = _mock_systems(monkeypatch)
    _, handlers = CommandRouter([roku], api_key="k")._build()
    assert handlers["roku_launch"]({"app": "netflix"}) == "Launching Netflix"
    assert "No app" in handlers["roku_launch"]({"app": "nonsense"})
    assert handlers["roku_button"]({"button": "home"}) == "Pressed home"
    assert "Unknown" in handlers["roku_button"]({"button": "explode"})


# --- Controller state machine ------------------------------------------------


class _FakeRouter:
    def __init__(self, available=True, reply="ok"):
        self._available = available
        self._reply = reply
    def available(self):
        return self._available
    def handle(self, text):
        return f"{self._reply}:{text}"


def test_controller_text_capture_and_cancel():
    vc = VoiceController([], router=_FakeRouter())
    vc.begin()
    assert vc.snapshot()[0] == "input"
    for ch in "hi":
        vc.feed_key(ord(ch))
    assert vc.snapshot()[1] == "hi"
    vc.feed_key(curses.KEY_BACKSPACE)
    assert vc.snapshot()[1] == "h"
    vc.feed_key(27)  # ESC
    assert vc.snapshot()[0] == "off"


def test_controller_think_worker_sets_result():
    vc = VoiceController([], router=_FakeRouter(reply="did"))
    vc._think_worker("turn on lights")
    mode, _, message, is_error = vc.snapshot()
    assert mode == "result" and message == "did:turn on lights" and not is_error


def test_controller_unavailable_shows_error():
    vc = VoiceController([], router=_FakeRouter(available=False))
    vc.begin()
    mode, _, message, is_error = vc.snapshot()
    assert mode == "result" and is_error and "ANTHROPIC_API_KEY" in message


def test_controller_result_dismissed_by_any_key():
    vc = VoiceController([], router=_FakeRouter())
    vc._set("result", "done", False)
    assert vc.feed_key(ord("x")) is True
    assert vc.snapshot()[0] == "off"
