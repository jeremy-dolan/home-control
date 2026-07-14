"""Tests for config loading and color-resolution helpers (no curses init)."""

from home_control import config, ui


def test_hex_rgb():
    assert ui._hex_rgb("#FF8C42") == (255, 140, 66)
    assert ui._hex_rgb("33AAFF") == (51, 170, 255)


def test_nearest_256_in_range():
    idx = ui._nearest_256(102, 45, 145)
    assert 16 <= idx <= 255
    # pure white and black map to cube corners
    assert ui._nearest_256(255, 255, 255) == 16 + 36 * 5 + 6 * 5 + 5
    assert ui._nearest_256(0, 0, 0) == 16


def test_pad_between():
    assert ui.pad_between("a", "b", 5) == "a   b"
    # overflow still leaves a single separating space
    assert ui.pad_between("aaa", "bbb", 4) == "aaa bbb"


def test_config_get_and_empty_as_unset(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[hue]\nbridge_ip = "10.0.0.5"\n\n'
        '[roku]\nip = ""\n\n'
        '[sonos]\nspeakers = [{ ip = "192.168.1.60" }, { ip = "192.168.1.61", name = "Kitchen" }]\n'
    )
    monkeypatch.setattr(config, "CONFIG_PATH", cfg)
    monkeypatch.setattr(config, "_cache", None)

    assert config.get("hue", "bridge_ip") == "10.0.0.5"
    assert config.get("roku", "ip", "AUTO") == "AUTO"          # empty string → default
    assert config.get("sonos", "speakers") == [
        {"ip": "192.168.1.60"}, {"ip": "192.168.1.61", "name": "Kitchen"}]
    assert config.get("midea", "missing", 42) == 42            # absent section → default


def test_config_autocreates(tmp_path, monkeypatch):
    cfg = tmp_path / "sub" / "config.toml"
    monkeypatch.setattr(config, "CONFIG_PATH", cfg)
    monkeypatch.setattr(config, "_cache", None)
    # Reading a missing config writes the template and still parses.
    assert config.get("hue", "bridge_ip") == "192.168.1.99"
    assert cfg.exists()
