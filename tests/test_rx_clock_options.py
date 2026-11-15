"""rx_clock / rx_data_delay validation (ESP32-S3 shared STD bus), without hardware."""

import importlib.util
from pathlib import Path

import pytest
from esphome import config_validation as cv
import esphome.final_validate as fv

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "esphome/components/esp_audio_stack"
spec = importlib.util.spec_from_file_location("rx_clock_schema", COMPONENT / "__init__.py")
schema = importlib.util.module_from_spec(spec)
spec.loader.exec_module(schema)


@pytest.fixture(autouse=True)
def final_validation_context():
    token = fv.full_config.set({})
    yield
    fv.full_config.reset(token)


def _on(monkeypatch, variant):
    monkeypatch.setattr(schema, "get_esp32_variant", lambda: variant)


@pytest.mark.parametrize("config", [
    {"sample_rate": 48000},
    {"sample_rate": 48000, "rx_clock": "fast"},
    {"sample_rate": 16000, "rx_clock": "fast", "rx_data_delay": "pos_edge"},
    {"sample_rate": 44100, "rx_data_delay": "neg_edge"},
])
def test_s3_shared_bus_accepts_options(monkeypatch, config):
    _on(monkeypatch, "ESP32S3")
    assert schema._final_validate(config) is config


@pytest.mark.parametrize("config, match", [
    ({"sample_rate": 44100, "rx_clock": "fast"}, "divides 120 MHz"),
    ({"sample_rate": 48000, "rx_clock": "fast", "i2s_mode": "secondary"}, "primary"),
    ({"sample_rate": 48000, "rx_clock": "fast", "use_tdm_reference": True}, "TDM"),
    ({"sample_rate": 48000, "rx_clock": "fast",
      "rx_bus": {"i2s_num": 0}, "tx_bus": {"i2s_num": 1}}, "rx_bus"),
])
def test_s3_fast_clock_rejects_unsupported_layouts(monkeypatch, config, match):
    _on(monkeypatch, "ESP32S3")
    with pytest.raises(cv.Invalid, match=match):
        schema._final_validate(config)


@pytest.mark.parametrize("config", [
    {"sample_rate": 48000, "rx_clock": "fast"},
    {"sample_rate": 48000, "rx_data_delay": "pos_edge"},
])
def test_options_are_s3_only(monkeypatch, config):
    _on(monkeypatch, "ESP32P4")
    with pytest.raises(cv.Invalid, match="ESP32-S3"):
        schema._final_validate(config)


def test_cpp_applies_options_only_on_s3():
    source = (COMPONENT / "esp_audio_stack.cpp").read_text()
    for marker in ("rx_cfg.clk_cfg.clk_src = I2S_CLK_SRC_PLL_240M", "hw->rx_timing.rx_sd_in_dm"):
        before = source[:source.index(marker)]
        assert before.rfind("#if CONFIG_IDF_TARGET_ESP32S3") > before.rfind("#endif")
