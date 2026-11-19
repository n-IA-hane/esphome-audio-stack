"""Resolve resource options through the native ESPHome loader and code generator."""

import subprocess
import sys

import pytest
import yaml

from test_facade_pcm_config import base_config


@pytest.mark.parametrize("memory", ["auto", "internal", "psram"])
def test_resource_options_reach_the_selected_owner(tmp_path, memory):
    config = base_config(tmp_path)
    config["external_components"][0]["components"].extend(["esp_afe", "esp_audio_capture"])
    config["esp_audio_stack"].update(id="bus", processor_id="afe", speaker_buffer_in_psram=False)
    config["esp_afe"] = {
        "id": "afe", "reconfigure_task_core": -1, "reconfigure_task_priority": 3,
        "reconfigure_task_stack_size": 16384, "reconfigure_task_stack_memory": memory,
    }
    config["esp_audio_capture"] = {
        "id": "capture", "esp_audio_stack_id": "bus", "microphone_id": "mic", "address": "127.0.0.1",
        "task_core": 1, "task_priority": 4, "task_stack_size": 6144,
    }
    path = tmp_path / "device.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    generated = subprocess.run([sys.executable, "-m", "esphome", "compile", "--only-generate", str(path)],
                               capture_output=True, text=True, timeout=90)
    assert generated.returncode == 0, generated.stdout + generated.stderr
    source = (tmp_path / "build/src/main.cpp").read_text()
    memory_code = {"auto": 0, "internal": 1, "psram": 2}[memory]
    for call in ("set_reconfigure_task_core(-1)", "set_reconfigure_task_priority(3)",
                 "set_reconfigure_task_stack_size(16384)",
                 f"set_reconfigure_task_stack_memory({memory_code})",
                 "set_speaker_buffer_in_psram(false)", "set_task_core(1)",
                 "set_task_priority(4)", "set_task_stack_size(6144)"):
        assert call in source, call


def test_explicit_psram_stack_requires_the_psram_component(tmp_path):
    config = base_config(tmp_path)
    del config["psram"]
    config["external_components"][0]["components"].append("esp_afe")
    config["esp_afe"] = {"id": "afe", "reconfigure_task_stack_memory": "psram"}
    path = tmp_path / "device.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    result = subprocess.run([sys.executable, "-m", "esphome", "config", str(path)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode != 0
    assert "psram" in result.stdout + result.stderr
