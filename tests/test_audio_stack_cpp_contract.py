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


def test_realtime_audio_loop_has_no_tick_delay_or_effect_allocator() -> None:
    cpp = read("audio_pipeline.cpp")
    alc = cpp[
        cpp.index("bool ESPAudioStack::apply_mic_alc_gain_(") :
        cpp.index("\n#ifdef USE_ESP_AUDIO_STACK_MONO_REF", cpp.index("bool ESPAudioStack::apply_mic_alc_gain_("))
    ]
    converter = cpp[
        cpp.index("bool ESPAudioStack::tx_bit_cvt_16_to_32_(") :
        cpp.index("\n#endif", cpp.index("bool ESPAudioStack::tx_bit_cvt_16_to_32_("))
    ]
    cold_allocate = cpp[
        cpp.index("bool ESPAudioStack::allocate_audio_buffers_(") :
        cpp.index("\nvoid ESPAudioStack::preallocate_audio_buffers_from_task_", cpp.index("bool ESPAudioStack::allocate_audio_buffers_("))
    ]

    assert "vTaskDelay(" not in cpp
    assert "esp_ae_alc_open" not in alc
    assert "esp_ae_alc_close" not in alc
    assert "esp_ae_bit_cvt_open" not in converter
    assert "esp_ae_bit_cvt_close" not in converter
    assert "esp_ae_alc_open" in cold_allocate
    assert "esp_ae_bit_cvt_open" in cold_allocate
    assert "frame_interval_avg_us" in cpp
    assert "t_frame_interval_max_us" in cpp


def test_idle_tx_completion_overflow_preserves_full_duplex_capture() -> None:
    """Clock-only DMA callbacks may outpace the audio task during Wi-Fi startup."""
    cpp = read("esp_audio_stack.cpp")
    header = read("esp_audio_stack.h")
    callback = cpp[
        cpp.index("bool IRAM_ATTR ESPAudioStack::tx_on_sent_callback") :
        cpp.index("bool ESPAudioStack::prepare_tx_completion_tracking_")
    ]

    assert "tx_completion_idle_event_drops_" in header
    assert "tx_completion_pending_real_records_.load" in callback
    assert "self->tx_completion_desync_ = true" in callback
    assert "self->tx_completion_idle_event_drops_.fetch_add" in callback
    assert "Discarded %u idle TX completion events" in cpp
