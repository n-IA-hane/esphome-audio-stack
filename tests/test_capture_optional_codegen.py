"""Exercise opt-in capture schemas/codegen without downloading or executing models."""
import json
from pathlib import Path
import subprocess
import sys

import pytest
from esphome.yaml_util import dump
from test_facade_pcm_config import base_config


@pytest.mark.parametrize("variant", ["normal", "planes", "boundaries", "test-input", "missing-afe"])
def test_diagnostic_options_are_explicit(tmp_path, variant):
    document = base_config(tmp_path)
    document["external_components"][0]["components"].append("esp_afe")
    document["esp_afe"] = {"id": "afe", "mic_num": 2, "se_enabled": True}
    document["esp_audio_stack"].update({"id": "bus", "processor_id": "afe", "output_sample_rate": 16000,
        "tdm_total_slots": 4, "tdm_mic_slots": [0, 2], "tdm_ref_slot": 1, "aec_reference": "ring_buffer"})
    if variant == "test-input":
        document["esp_audio_stack"]["enable_test_input"] = True
    if variant in ("planes", "boundaries", "missing-afe"):
        document["external_components"][0]["components"].append("esp_audio_capture")
        document["esp_audio_capture"] = {"id": "capture", "esp_audio_stack_id": "bus", "microphone_id": "mic",
            "afe_id": "afe", "address": "127.0.0.1", "points": ["raw_mic", "reference", "processor_input",
                "processor_output", "output", "afe_separated", "afe_selected"]}
    if variant == "missing-afe":
        del document["esp_audio_capture"]["afe_id"]
    if variant == "boundaries":
        # Opaque bytes exercise code generation only, never firmware or inference.
        (tmp_path / "fixture.tflite").write_bytes(b"codegen-only")
        manifest = {"type": "micro", "version": 2, "model": "fixture.tflite", "author": "fixture",
            "wake_word": "fixture", "trained_languages": ["en"], "micro": {"feature_step_size": 10,
                "tensor_arena_size": 32768, "probability_cutoff": .9, "sliding_window_size": 5,
                "minimum_esphome_version": "2025.1.0"}}
        (tmp_path / "model.json").write_text(json.dumps(manifest))
        document["micro_wake_word"] = {"microphone": "mic", "models": [{"model": str(tmp_path / "model.json")}]}
        document["esp_audio_capture"]["boundary_probes"] = True
    config = tmp_path / "device.yaml"
    config.write_text(dump(document))
    result = subprocess.run([sys.executable, "-m", "esphome", "compile", "--only-generate", str(config)],
        capture_output=True, text=True, timeout=90)
    if variant == "missing-afe":
        assert result.returncode != 0
        assert "require afe_id" in result.stdout + result.stderr
        return
    assert result.returncode == 0, result.stdout + result.stderr
    definitions = (tmp_path / "build/src/esphome/core/defines.h").read_text()
    expected = {"USE_ESP_AUDIO_STACK_CAPTURE": variant in ("planes", "boundaries"),
        "USE_ESP_AUDIO_STACK_AFE_OUTPUT_CAPTURE": variant in ("planes", "boundaries"),
        "USE_ESP_AUDIO_BOUNDARY_PROBES": variant == "boundaries",
        "USE_ESP_AUDIO_STACK_TEST_INPUT": variant == "test-input"}
    for flag, enabled in expected.items():
        assert (f"#define {flag}\n" in definitions) == enabled
    assert "#define USE_ESP_AUDIO_STACK_TELEMETRY\n" not in definitions


def test_afe_dump_api_remains_callable_without_diagnostics(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source = (root / "esphome/components/esp_afe/esp_afe.cpp").read_text()
    start = source.index("void EspAfe::dump_diagnostics() {")
    body = source[start:source.index("\n}\n", start) + 2]
    cpp = tmp_path / "disabled.cpp"
    cpp.write_text("#include <cassert>\nint logs=0;\n#define ESP_LOGI(...) (++logs)\n"
        "struct EspAfe {void dump_diagnostics();};\n" + body + "\nint main(){EspAfe x;x.dump_diagnostics();assert(logs==1);}")
    binary = tmp_path / "disabled"
    subprocess.run(["g++", "-std=c++17", "-Wall", "-Werror", str(cpp), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
