"""Run ESPHome's actual final-validation phase, where CORE.config is unset."""

from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("variant,valid,message", [
    ("valid", True, ""),
    ("missing_slots", False, "requires tdm_mic_slots"),
    ("disabled_agc_support", False, "conflicts with dual-mic AGC"),
])
def test_real_final_validation_context(tmp_path, variant, valid, message):
    text = (ROOT / "tests/components/esp_audio_stack/test.esp32-s3-std-dual-mic-idf.yaml").read_text()
    if variant == "missing_slots":
        text = text.replace("  rx_mic_slots: [left, right]\n", "")
    if variant == "disabled_agc_support":
        text = text.replace("  agc_enabled: false", "  agc_enabled: true\n  post_afe_agc_support: false")
    if variant == "valid":
        text += """
sensor:
  - platform: esp_audio_stack
    esp_audio_stack_id: audio_stack
    std_slot_levels:
      - slot: left
        name: Left input level
      - slot: right
        name: Right input level
"""
    text += f"""
external_components:
  - source:
      type: local
      path: {ROOT / 'esphome/components'}
    components: [esp_audio_stack, esp_afe]
"""
    config = tmp_path / "audio.yaml"
    config.write_text(text)
    result = subprocess.run([sys.executable, "-m", "esphome", "config", str(config)],
                            capture_output=True, text=True, timeout=45)
    output = result.stdout + result.stderr
    assert (result.returncode == 0) is valid, output
    assert "Traceback" not in output
    if message:
        assert message in output


def test_unnamed_stack_binds_endpoints_actions_and_idle_condition(tmp_path):
    """Exercise ESPHome's actual ID resolution without a named stack."""
    import re

    text = (ROOT / "examples/00-generic-i2s-duplex.yaml").read_text().replace(
        "github://n-IA-hane/esphome-audio-stack@dev", str(ROOT / "esphome/components")
    )
    text += """
button:
  - platform: template
    name: Exercise audio actions
    on_press:
      - esp_audio_stack.stop:
      - wait_until:
          condition:
            esp_audio_stack.is_idle:
          timeout: 2s
      - esp_audio_stack.start:
      - esp_audio_stack.dump_diagnostics:
"""
    path = tmp_path / "unnamed.yaml"
    path.write_text(text)
    result = subprocess.run(
        [sys.executable, "-m", "esphome", "compile", str(path), "--only-generate"],
        capture_output=True, text=True, timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    generated = next(tmp_path.rglob("main.cpp")).read_text()
    parents = re.findall(r"->set_parent\(([^)]+)\)", generated)
    assert len(parents) >= 6
    assert len(set(parents)) == 1


def test_component_rejects_retired_builder_before_hardware_validation(monkeypatch):
    """A standalone consumer gets the promised version error, not a C++ failure."""
    import importlib.util
    import esphome.config_validation as cv

    spec = importlib.util.spec_from_file_location(
        'audio_stack_minimum_config', ROOT / 'esphome/components/esp_audio_stack/__init__.py'
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    minimum = module.CONFIG_SCHEMA.validators[0]
    monkeypatch.setattr(cv, 'ESPHOME_VERSION', '2026.9.1')
    with pytest.raises(cv.Invalid, match='at least ESPHome version 2026.10.0'):
        minimum({})
    for version in ('2026.10.0', '2026.10.1', '2026.11.0'):
        monkeypatch.setattr(cv, 'ESPHOME_VERSION', version)
        value = {}
        assert minimum(value) is value
