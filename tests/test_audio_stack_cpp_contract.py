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


def _dual_mic_slots_ok(
    processor_mic_channels: int,
    use_tdm_bus: bool,
    tdm_second_mic_slot: int,
    rx_slot_mode_stereo: bool,
    std_second_mic_slot: int,
) -> bool:
    """Mirror audio_session_ dual-mic slot gating (must stay in lockstep with C++)."""
    if processor_mic_channels <= 1:
        return True
    have_tdm_dual_mic = use_tdm_bus and tdm_second_mic_slot >= 0
    have_std_dual_mic = rx_slot_mode_stereo and std_second_mic_slot >= 0
    return have_tdm_dual_mic or have_std_dual_mic


def test_std_stereo_dual_mic_does_not_require_tdm() -> None:
    cpp = read("audio_pipeline.cpp")
    header = read("esp_audio_stack.h")
    init = (AUDIO_STACK / "__init__.py").read_text(encoding="utf-8")

    assert "dual-mic processor requires TDM microphone slots or STD rx_mic_slots" in cpp
    assert 'alloc_fail("dual-mic processor requires TDM microphone slots")' not in cpp
    assert "have_std_dual_mic = ctx.rx_slot_mode_stereo && ctx.std_second_mic_slot >= 0" in cpp
    assert "have_tdm_dual_mic = ctx.use_tdm_bus && ctx.tdm_second_mic_slot >= 0" in cpp

    stereo = cpp[
        cpp.index("bool ESPAudioStack::process_rx_stereo_slot_(") : cpp.index(
            "bool ESPAudioStack::process_rx_mono_effects_("
        )
    ]
    assert "SPH0645" in stereo
    assert "dc_primary_" in stereo
    assert "dc_secondary_" in stereo
    assert "L-R canceller" in stereo
    assert "ctx.processor_input = ctx.processor_mic_buffer" in stereo
    assert "process_multi_32" in stereo
    assert "num_mic_ch" not in stereo or ", 2)" in stereo
    assert "const uint8_t mic_offset = this->mic_channel_right_ ? 1 : 0;" in stereo
    assert "uint8_t ch_offsets[1] = {mic_offset};" in stereo
    assert "uint8_t ch_offsets[2] = {ctx.std_primary_mic_slot, static_cast<uint8_t>(ctx.std_second_mic_slot)};" in stereo

    dc = cpp[
        cpp.index("void ESPAudioStack::apply_input_conditioning_(") : cpp.index(
            "void ESPAudioStack::update_tdm_slot_levels_"
        )
    ]
    assert "dc_primary_.process" in dc
    assert "dc_secondary_.process" in dc
    assert "L-R canceller" in dc

    assert "void set_std_mic_slots" in header
    assert "std_second_mic_slot_{-1}" in header
    assert "CONF_RX_MIC_SLOTS = \"rx_mic_slots\"" in init
    assert "rx_mic_slots requires rx_slot_mode: stereo" in init
    assert "use_stereo_aec_reference is" in init

    # Single-mic stereo-slot still allocates; dual-mic STD does not trip the
    # old TDM-only fail; TDM dual-mic still requires two TDM slots.
    assert _dual_mic_slots_ok(1, False, -1, True, -1)
    assert _dual_mic_slots_ok(2, False, -1, True, 1)
    assert not _dual_mic_slots_ok(2, False, -1, True, -1)
    assert not _dual_mic_slots_ok(2, False, -1, False, 1)
    assert _dual_mic_slots_ok(2, True, 2, False, -1)
    assert not _dual_mic_slots_ok(2, True, -1, False, -1)


def test_idle_teardown_false_does_not_queue_i2s_delete() -> None:
    """Voice satellites restart MWW immediately; deleting I2S DMA then fails RX alloc."""
    cpp = read("esp_audio_stack.cpp")
    header = read("esp_audio_stack.h")
    init = (AUDIO_STACK / "__init__.py").read_text(encoding="utf-8")
    stop = cpp[cpp.index("void ESPAudioStack::stop()") : cpp.index("bool ESPAudioStack::stop_and_wait")]

    assert "void set_idle_teardown" in header
    assert "bool idle_teardown_{true}" in header
    assert "CONF_IDLE_TEARDOWN = \"idle_teardown\"" in init
    assert "Stopping audio stack (keeping I2S)" in stop
    assert "this->idle_teardown_" in stop
    assert stop.index("if (this->idle_teardown_)") < stop.index("teardown_pending_.store(true")


def test_tx_aec_ref_conversion_failure_does_not_stop_session() -> None:
    """First duplex TX used to kill I2S when esp_ae FIR putbuf alloc failed."""
    cpp = read("audio_pipeline.cpp")
    fx = read("audio_effects_rate_converter.cpp")
    assert "dropping this frame's AEC ref" in cpp
    assert "TX AEC reference rate conversion failed; stopping audio session" not in cpp
    impl = fx[fx.index("class AudioEffectsRateConverterImpl") : fx.index("class MultiChannelAudioEffectsRateConverterImpl")]
    assert '"warmup"' in impl
    assert "this->warmed_" in impl
