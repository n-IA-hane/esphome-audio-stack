#!/usr/bin/env python3
"""Static contract checks for ESP Audio Stack hot paths."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AUDIO_STACK = ROOT / "esphome" / "components" / "esp_audio_stack"


def read(name: str) -> str:
    return (AUDIO_STACK / name).read_text(encoding="utf-8")


def test_tdm_16bit_rx_extracts_only_selected_channels() -> None:
    cpp = read("audio_effects_rate_converter.cpp")

    prepare = cpp[cpp.index("bool prepare(") : cpp.index("bool process_multi(", cpp.index("bool prepare("))]
    assert "const uint8_t deintlv_channels = source_32bit ? source_channels : nch;" in prepare
    assert "ensure_deintlv_buffers_(deintlv_channels, in_count)" in prepare

    deinterleave = cpp[cpp.index("bool deinterleave_selected_(") : cpp.index("bool extract_selected_channels_(")]
    assert "ensure_deintlv_buffers_(static_cast<uint8_t>(stride), in_count)" in deinterleave
    assert "return this->extract_selected_channels_" in deinterleave

    extract = cpp[cpp.index("bool extract_selected_channels_(") : cpp.index("bool ensure_bit_conversion_(")]
    assert "ensure_deintlv_buffers_(this->channels_, in_count)" in extract
    assert "out[i] = in[i * stride + offset];" in extract
