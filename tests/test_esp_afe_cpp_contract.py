#!/usr/bin/env python3
"""Static contract checks for the ESP AFE processor."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AFE = ROOT / "esphome" / "components" / "esp_afe"
AEC = ROOT / "esphome" / "components" / "esp_aec"


def read(name: str) -> str:
    return (AFE / name).read_text(encoding="utf-8")


def read_aec(name: str) -> str:
    return (AEC / name).read_text(encoding="utf-8")


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


def test_gmf_dual_mic_feed_uses_direct_ring_slots() -> None:
    cpp = read("esp_afe.cpp")
    header = read("esp_afe.h")

    assert "xRingbufferSendAcquire" in cpp
    assert "xRingbufferSendComplete" in cpp
    assert "acquire_gmf_feed_slot_" in header
    assert "commit_gmf_feed_slot_" in header

    gmf_start = cpp.index("const bool needs_feed_staging = process_chunksize != feed_chunksize;")
    gmf_end = cpp.index("instance->feed_buf = feed_buf;", gmf_start)
    gmf_build = cpp[gmf_start:gmf_end]
    assert "const bool needs_feed_staging = process_chunksize != feed_chunksize;" in gmf_build
    assert "if (needs_feed_staging)" in gmf_build
    assert "Failed to allocate staged feed buffer" in gmf_build

    process = cpp[cpp.index("bool EspAfe::process(") :]
    assert "const bool gmf_direct_frame = gmf_path && !direct_path && offset == 0 && qs == fs;" in process
    assert "stage_afe_input_frame(static_cast<int16_t *>(gmf_slot)" in process


def test_esp_afe_uses_current_espressif_afe_dependencies() -> None:
    init = read("__init__.py")
    aec_init = read_aec("__init__.py")

    assert 'add_idf_component(name="espressif/esp-sr", ref="^2.4.6")' in init
    assert 'name="espressif/gmf_ai_audio"' in init
    assert 'repo="https://github.com/n-IA-hane/esp-gmf.git"' in init
    assert 'ref="gmf-ai-audio-esp-sr-2.4.6"' in init
    assert 'path="elements/gmf_ai_audio"' in init
    assert not (AFE / "idf_components" / "gmf_ai_audio").exists()
    assert 'ref="0.8.3"' not in init
    assert 'add_idf_component(name="espressif/esp-sr", ref="^2.4.6")' in aec_init
    assert 'ref="^2.4.4"' not in aec_init
