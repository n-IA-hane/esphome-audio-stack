"""Run the real ESPHome loader/code generator for optional audio features."""

from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "variant",
    [
        "aec",
        "afe",
        "raw",
        "default",
        "dual-rejected",
        "codec-rejected",
        "reference-disabled-rejected",
        "reference-outside-mask-rejected",
        "binding-rejected",
    ],
)
def test_optional_feature_composition(tmp_path, variant):
    config = (ROOT / "examples/04-dual-mic-downmix.yaml").read_text()
    config = config.replace("github://n-IA-hane/esphome-audio-stack@dev", str(ROOT / "esphome/components"))
    config = config.replace("!secret wifi_ssid", "test-network").replace(
        "!secret wifi_password", "test-password"
    )
    config = config.replace(
        "  name: audio-stack-downmix",
        f"  name: audio-stack-downmix\n  build_path: {tmp_path}/build",
    )
    if variant in ("afe", "dual-rejected"):
        config = config.replace(
            "esp_aec, esp_audio_capture", "esp_afe, esp_audio_capture"
        )
        config = config.replace(
            "esp_aec:\n  id: processor\n  sample_rate: 16000\n  mode: fd_low_cost\n  filter_length: 4",
            "esp_afe:\n  id: processor\n  mic_num: "
            + ("2" if variant == "dual-rejected" else "1"),
        )
        if variant == "dual-rejected":
            config = config.replace("  mic_num: 2", "  mic_num: 2\n  se_enabled: true")
    if variant == "raw":
        config = config.replace("  processor_id: processor\n", "")
        config = config.replace("esp_audio_stack, esp_aec,", "esp_audio_stack,")
        config = config.replace(
            "esp_aec:\n  id: processor\n  sample_rate: 16000\n  mode: fd_low_cost\n  filter_length: 4\n",
            "",
        )
    if variant == "default":
        config = config[: config.index("number:\n")]
        config = config.replace("  mic_mix: average\n", "").replace(
            ", esp_audio_capture", ""
        )
    if variant == "codec-rejected":
        config = config.replace("      ref_channel: 2\n", "")
    if variant == "reference-disabled-rejected":
        config = config.replace("      mic_selected: 15", "      mic_selected: 3")
    if variant == "reference-outside-mask-rejected":
        config = config.replace("      ref_channel: 2", "      ref_channel: 7")
    if variant == "binding-rejected":
        config = config.replace("  microphone_id: mic", "  microphone_id: output")
    path = tmp_path / "device.yaml"
    path.write_text(config)
    result = subprocess.run(
        [sys.executable, "-m", "esphome", "compile", "--only-generate", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if variant.endswith("-rejected"):
        assert result.returncode != 0
        expected = {
            "dual-rejected": "mic_num: 1",
            "codec-rejected": "physical ref_channel",
            "reference-disabled-rejected": "physical ref_channel",
            "reference-outside-mask-rejected": "physical ref_channel",
            "binding-rejected": "type",
        }[variant]
        assert expected in result.stdout + result.stderr, result.stdout + result.stderr
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        defines = (tmp_path / "build/src/esphome/core/defines.h").read_text()
        for feature in ("MIC_DOWNMIX", "CAPTURE", "HARDWARE_GAIN_CONTROL"):
            assert (f"#define USE_ESP_AUDIO_STACK_{feature}" in defines) == (
                variant != "default"
            )
