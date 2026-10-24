"""Native ESPHome validates exact facade capabilities before consumer codegen."""

from pathlib import Path
import subprocess
import sys

import pytest
import yaml
import esphome

ROOT = Path(__file__).resolve().parents[1]


def test_standard_audio_providers_are_not_shadowed():
    """Keep the standalone integration on the released ESPHome providers."""
    component_root = ROOT / "esphome/components"
    for provider in ("audio", "resampler", "mixer", "speaker_source", "voice_assistant", "micro_wake_word", "ota"):
        assert not (component_root / provider).exists(), f"local {provider} shadows ESPHome"


def base_config(tmp_path):
    document = {
        "esphome": {
            "name": "audio-pcm-contract",
            "build_path": str(tmp_path / "build"),
        },
        "esp32": {"board": "esp32-s3-devkitc-1", "framework": {"type": "esp-idf"}},
        "psram": {"mode": "octal", "speed": "80MHz"},
        "logger": {},
        "api": {},
        "wifi": {"ssid": "fixture-network", "password": "fixture-password"},
        "external_components": [
            {
                "source": str(ROOT / "esphome/components"),
                "components": ["esp_audio_stack"],
            }
        ],
        "esp_audio_stack": {
            "sample_rate": 48000,
            "i2s_bclk_pin": 6,
            "i2s_lrclk_pin": 7,
            "i2s_din_pin": 4,
            "i2s_dout_pin": 8,
        },
        "microphone": [{"platform": "esp_audio_stack", "id": "mic"}],
        "speaker": [{"platform": "esp_audio_stack", "id": "hw"}],
    }
    return document


@pytest.mark.parametrize("consumer", ["assist", "resampler"])
@pytest.mark.parametrize("reversed_order", [False, True])
def test_mismatched_facade_pcm_is_rejected(tmp_path, consumer, reversed_order):
    document = base_config(tmp_path)
    if consumer == "assist":
        document["voice_assistant"] = {"microphone": "mic"}
    else:
        document["speaker"].append(
            {
                "platform": "resampler",
                "id": "converted",
                "output_speaker": "hw",
                "sample_rate": 16000,
                "bits_per_sample": 16,
                "num_channels": 1,
            }
        )
    if reversed_order:
        document = dict(reversed(list(document.items())))
    path = tmp_path / "device.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    result = subprocess.run(
        [sys.executable, "-m", "esphome", "config", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0, "incompatible 48/16 kHz connection was accepted"
    assert (
        "sample rate" in result.stdout + result.stderr
        or "sample_rate value must" in result.stdout + result.stderr
    )


@pytest.mark.parametrize("layout", ["mono", "stereo", "tdm"])
@pytest.mark.parametrize("reversed_order", [False, True])
def test_exact_rates_and_channels_validate_with_generated_parent_id(
    tmp_path, layout, reversed_order
):
    document = base_config(tmp_path)
    document["esp_audio_stack"]["output_sample_rate"] = 16000
    if layout == "stereo":
        document["esp_audio_stack"]["num_channels"] = 2
        document["esp_audio_stack"]["speaker_channels"] = 2
    elif layout == "tdm":
        document["esp_audio_stack"].update(
            {"tdm_mic_slots": [0, 2], "tdm_total_slots": 4}
        )
    document["voice_assistant"] = {"microphone": "mic"}
    document["speaker"].append(
        {
            "platform": "resampler",
            "id": "converted",
            "output_speaker": "hw",
            "sample_rate": 48000,
            "bits_per_sample": 16,
            "num_channels": 2 if layout == "stereo" else 1,
        }
    )
    if reversed_order:
        document = dict(reversed(list(document.items())))
    path = tmp_path / "device.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    result = subprocess.run(
        [sys.executable, "-m", "esphome", "config", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    output = yaml.safe_load(result.stdout)
    mic, speaker = output["microphone"][0], output["speaker"][0]
    assert mic["min_sample_rate"] == mic["max_sample_rate"] == 16000
    assert mic["min_channels"] == mic["max_channels"] == 1
    assert speaker["min_sample_rate"] == speaker["max_sample_rate"] == 48000
    assert (
        speaker["min_channels"]
        == speaker["max_channels"]
        == (2 if layout == "stereo" else 1)
    )


@pytest.mark.parametrize("channels", [1, 2])
def test_facades_generate_with_released_mixer_resampler_and_assist(tmp_path, channels):
    """Generate the real composition and identify the standard C++ providers."""
    document = base_config(tmp_path)
    document["esp_audio_stack"].update(
        {"output_sample_rate": 16000, "speaker_channels": channels, "num_channels": channels}
    )
    document["voice_assistant"] = {"microphone": "mic", "speaker": "converted"}
    document["speaker"].extend(
        [
            {
                "platform": "mixer",
                "id": "mix",
                "output_speaker": "hw",
                "num_channels": channels,
                "source_speakers": [{"id": "voice_input"}, {"id": "media_input"}],
            },
            {
                "platform": "resampler",
                "id": "converted",
                "output_speaker": "voice_input",
                "sample_rate": 48000,
                "bits_per_sample": 16,
                "num_channels": channels,
            },
        ]
    )
    path = tmp_path / "device.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    result = subprocess.run(
        [sys.executable, "-m", "esphome", "compile", "--only-generate", str(path)],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    generated = tmp_path / "build/src/esphome/components"
    released = Path(esphome.__file__).resolve().parent / "components"
    for provider in ("audio", "resampler", "mixer", "voice_assistant"):
        files = sorted(
            source for source in (generated / provider).rglob("*")
            if source.suffix in {".cpp", ".h"}
        )
        assert files, f"{provider} was not generated"
        for source in files:
            relative = source.relative_to(generated)
            assert source.read_bytes() == (released / relative).read_bytes(), relative
    own_speaker = generated / "esp_audio_stack/speaker/esp_audio_stack_speaker.cpp"
    assert own_speaker.read_bytes() == (
        ROOT / "esphome/components/esp_audio_stack/speaker/esp_audio_stack_speaker.cpp"
    ).read_bytes()
