#!/usr/bin/env python3
"""Static contract checks for the ESP AFE processor."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AFE = ROOT / "esphome" / "components" / "esp_afe"


def read(name: str) -> str:
    return (AFE / name).read_text(encoding="utf-8")


def test_single_mic_aec_toggle_rebuilds_instead_of_live_disabling() -> None:
    cpp = read("esp_afe.cpp")
    header = read("esp_afe.h")

    assert "single-mic AFE rebuild" in cpp
    assert "cfg->aec_init = afe_mic_channels >= 2 || this->aec_enabled_.load" in cpp
    assert "return this->mic_num_ <= 1 ? FeatureControl::RESTART_REQUIRED : FeatureControl::LIVE_TOGGLE;" in cpp
    assert "rebuild-only on the ESP-SR single-mic direct path" in header

    install_start = cpp.index("bool EspAfe::install_instance_(")
    install_end = cpp.index("\nEspAfe::AfeInstance EspAfe::detach_instance_", install_start)
    install = cpp[install_start:install_end]
    assert "direct_iface_->disable_aec" not in install
