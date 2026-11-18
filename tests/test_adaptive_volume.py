"""Check the optional playback gain and the real ESPHome switch schema."""
from pathlib import Path
import subprocess
import sys

import pytest
from esphome.yaml_util import dump

from test_facade_pcm_config import base_config

ROOT = Path(__file__).resolve().parents[1]


def test_bounded_playback_gain(tmp_path):
    binary = tmp_path / "adaptive"
    subprocess.run([
        "c++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
        "-I", str(ROOT / "esphome/components/esp_audio_stack"),
        str(ROOT / "tests/fixtures/adaptive_volume.cpp"), "-o", str(binary),
    ], check=True)
    subprocess.run([str(binary)], check=True)


@pytest.mark.parametrize(("case", "error"), [
    ("off", None),
    ("switch", None),
    ("levels", "loud_level must be greater"),
    ("speaker-only", "requires both microphone input and speaker output"),
    ("duplicate", "one adaptive_volume switch per audio stack"),
])
def test_adaptive_switch_codegen(tmp_path, case, error):
    config = base_config(tmp_path)
    if case != "off":
        option = {"name": "Adaptive volume"}
        config["switch"] = [{"platform": "esp_audio_stack", "adaptive_volume": option}]
        if case == "levels":
            option.update(quiet_level=-30, loud_level=-45)
        if case == "speaker-only":
            del config["esp_audio_stack"]["i2s_din_pin"]
            del config["microphone"]
        if case == "duplicate":
            config["switch"].append({"platform": "esp_audio_stack", "adaptive_volume": {"name": "Second"}})
    path = tmp_path / "board.yaml"
    path.write_text(dump(config))
    result = subprocess.run(
        [sys.executable, "-m", "esphome", "compile", "--only-generate", str(path)],
        capture_output=True, text=True, timeout=90,
    )
    output = result.stdout + result.stderr
    if error:
        assert result.returncode != 0 and error in output, output
        return
    assert result.returncode == 0, output
    source = tmp_path / "build/src"
    assert ("#define USE_ESP_AUDIO_STACK_ADAPTIVE_VOLUME" in (source / "esphome/core/defines.h").read_text()) == (case == "switch")
    if case == "switch":
        generated = (source / "main.cpp").read_text()
        assert "configure_adaptive_volume(" in generated
        assert "SWITCH_RESTORE_DEFAULT_OFF" in generated
