"""Each panel's in-app help (``help_notes()``) must appear in README's Device support."""

import re
from pathlib import Path

import pytest

from home_control import config
from home_control.systems import build_systems

README = Path(__file__).resolve().parent.parent / "README.md"

# System.name -> its "### " heading under README's "## Device support".
README_HEADINGS = {
    "Router": "Router: Verizon Fios CR1000A",
    "Lighting": "Lighting: Hue",
    "Roku": "Media player: Roku",
    "Sonos": "Sound system: Sonos",
    "Midea AC": "HVAC: Midea",
}


def _device_sections() -> dict[str, str]:
    support = README.read_text(encoding="utf-8").split("\n## Device support\n", 1)[1]
    support = support.split("\n## ", 1)[0]
    parts = re.split(r"^### (.+)$", support, flags=re.M)
    return {parts[i].strip(): parts[i + 1] for i in range(1, len(parts), 2)}


def _plain(text: str) -> str:
    """Drop the Markdown only README has (links, code spans, emphasis) and fold whitespace."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = text.replace("`", "").replace("*", "")
    return " ".join(text.split())


@pytest.fixture
def documented(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME_CONTROL_MOCK", "1")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(config, "_cache", None)
    return [s for s in build_systems() if s.help_notes()]


def test_every_panel_with_help_has_a_readme_section(documented):
    sections = _device_sections()
    for system in documented:
        assert system.name in README_HEADINGS, f"add {system.name!r} to README_HEADINGS"
        assert README_HEADINGS[system.name] in sections, (
            f"README has no '### {README_HEADINGS[system.name]}' under ## Device support"
        )


def test_help_notes_appear_verbatim_in_readme(documented):
    sections = _device_sections()
    missing = [
        f"{system.name} (README '### {README_HEADINGS[system.name]}'): {note[:70]}..."
        for system in documented
        for note in system.help_notes()
        if _plain(note) not in _plain(sections[README_HEADINGS[system.name]])
    ]
    assert not missing, "help_notes() paragraphs missing from README — keep them in sync:\n" + "\n".join(missing)
