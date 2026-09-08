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
    assert "isr_load_u32(self->tx_completion_pending_real_records_)" in callback
    assert "self->tx_completion_desync_ = true" in callback
    assert "isr_increment_u32(self->tx_completion_idle_event_drops_)" in callback
    assert "Discarded %u idle TX completion events" in cpp


def test_tx_completion_tracking_is_session_scoped() -> None:
    """Late IDF callbacks from a stopped session cannot poison the next call."""
    cpp = read("esp_audio_stack.cpp")
    header = read("esp_audio_stack.h")
    callback = cpp[
        cpp.index("bool IRAM_ATTR ESPAudioStack::tx_on_sent_callback") :
        cpp.index("bool ESPAudioStack::prepare_tx_completion_tracking_")
    ]
    stop = cpp[cpp.index("void ESPAudioStack::stop()") : cpp.index("bool ESPAudioStack::register_mic_consumer")]
    enable = cpp[cpp.index("bool ESPAudioStack::enable_i2s_channels_()") : cpp.index("void ESPAudioStack::close_audio_io_")]

    assert "std::atomic<bool> tx_completion_tracking_active_{false}" in header
    assert "isr_load_flag(self->tx_completion_tracking_active_)" in callback
    assert stop.index("tx_completion_tracking_active_.store(false") < stop.index(
        "audio_stack_running_.store(false"
    )
    assert "tx_completion_tracking_active_.store(true" in enable
    assert "tx_completion_tracking_active_.store(false" in cpp[
        cpp.index("void ESPAudioStack::reset_tx_completion_tracking_") :
        cpp.index("void ESPAudioStack::dispatch_speaker_output_callbacks_")
    ]
    failure = cpp[
        cpp.index("void ESPAudioStack::fail_tx_completion_tracking_") :
        cpp.index("bool ESPAudioStack::wait_audio_task_state_")
    ]
    assert "tx_completion_tracking_active_.store(false" in failure
    assert "has_i2s_error_.store(true" in failure
    assert "audio_stack_running_.store(false" in failure
    assert "teardown_pending_.store(true" in failure


def test_tx_completion_isr_never_calls_into_flash() -> None:
    """The IRAM-safe I2S callback runs while the flash cache is suspended.

    std::atomic<T> member functions are ordinary inline functions; at -Os GCC may
    emit them out of line in .flash.text, and an IRAM ISR calling into flash during
    a flash write deadlocks the ESP32-P4 silently (watchdog reset, no panic). The
    callback must therefore use the compiler builtins, which always expand inline.
    """
    cpp = read("esp_audio_stack.cpp")
    helpers = cpp[
        cpp.index("static inline bool IRAM_ATTR isr_load_flag(") :
        cpp.index("bool IRAM_ATTR ESPAudioStack::tx_on_sent_callback")
    ]
    callback = cpp[
        cpp.index("bool IRAM_ATTR ESPAudioStack::tx_on_sent_callback") :
        cpp.index("bool ESPAudioStack::prepare_tx_completion_tracking_")
    ]

    assert "__atomic_load_n(" in helpers
    assert "__ATOMIC_ACQUIRE" in helpers
    assert "__atomic_fetch_add(" in helpers
    assert "static_assert(sizeof(std::atomic<bool>) == sizeof(bool)" in cpp
    for forbidden in (".load(", ".store(", ".fetch_add(", ".exchange(", "std::memory_order"):
        assert forbidden not in callback, forbidden


def test_children_can_restart_after_parent_i2s_recovery() -> None:
    """A parent I2S fault is latched for visibility but cannot deadlock restart."""
    mic = read("microphone/esp_audio_stack_microphone.cpp")
    speaker = read("speaker/esp_audio_stack_speaker.cpp")
    mic_header = read("microphone/esp_audio_stack_microphone.h")
    speaker_header = read("speaker/esp_audio_stack_speaker.h")

    for source, header in ((mic, mic_header), (speaker, speaker_header)):
        assert "bool i2s_error_latched_{false}" in header
        assert "else if (this->i2s_error_latched_)" in source
        assert "this->status_clear_error();" in source
        assert "this->status_has_error() && !this->i2s_error_latched_" in source


def test_speaker_output_callbacks_follow_i2s_completion_not_buffer_acceptance() -> None:
    """Sendspin/mixer timing must advance only after DMA reports playback."""
    stack_cpp = read("esp_audio_stack.cpp")
    pipeline_cpp = read("audio_pipeline.cpp")
    speaker_cpp = read("speaker/esp_audio_stack_speaker.cpp")

    dma_callback = stack_cpp[
        stack_cpp.index("bool IRAM_ATTR ESPAudioStack::tx_on_sent_callback") :
        stack_cpp.index("bool ESPAudioStack::prepare_tx_completion_tracking_")
    ]
    completion_drain = stack_cpp[
        stack_cpp.index("void ESPAudioStack::drain_tx_completion_events_") :
        stack_cpp.index("bool ESPAudioStack::queue_tx_completion_record_")
    ]
    dma_write = pipeline_cpp[
        pipeline_cpp.index("bool ESPAudioStack::write_tx_dma_blocks_") :
        pipeline_cpp.index("void ESPAudioStack::process_tx_clock_only_")
    ]
    public_play = speaker_cpp[
        speaker_cpp.index("size_t ESPAudioStackSpeaker::play(const uint8_t *data, size_t length)") :
        speaker_cpp.index("bool ESPAudioStackSpeaker::has_buffered_data()")
    ]

    assert "add_speaker_output_callback" in speaker_cpp
    assert "audio_output_callback_.call(frames, timestamp)" in speaker_cpp
    assert "xQueueSendToBackFromISR" in dma_callback
    assert "dispatch_speaker_output_callbacks_" in completion_drain
    assert "record.real_frames" in completion_drain
    assert "record.trailing_silence_frames" in completion_drain

    # Every real write is paired with its completion record before submission
    # to IDF. Merely accepting bytes into the public speaker buffer must not
    # advance Sendspin/mixer playback time.
    assert dma_write.index("queue_tx_completion_record_(record)") < dma_write.index(
        "write_tx_frame_(ctx, bytes + offset"
    )
    assert "dispatch_speaker_output_callbacks_" not in dma_write
    assert "audio_output_callback_" not in public_play


def test_failed_tx_write_cannot_leave_stale_completion_metadata() -> None:
    """A reserved DMA record must never be consumed by a later successful write."""
    pipeline_cpp = read("audio_pipeline.cpp")
    dma_write = pipeline_cpp[
        pipeline_cpp.index("bool ESPAudioStack::write_tx_dma_blocks_") :
        pipeline_cpp.index("void ESPAudioStack::process_tx_clock_only_")
    ]

    # The record must remain queued before the blocking IDF write: on_sent may
    # run before i2s_channel_write() returns. If the write then fails, however,
    # tracking is irrecoverably ambiguous and the pipeline must stop instead of
    # letting a later DMA completion consume stale metadata.
    assert dma_write.index("queue_tx_completion_record_(record)") < dma_write.index(
        "write_tx_frame_(ctx, bytes + offset"
    )
    assert dma_write.count("mark_tx_completion_desync_") == 2
    assert "if (!this->write_tx_frame_(ctx, tx_data, tx_bytes))" in dma_write
    assert "if (!this->write_tx_frame_(ctx, bytes + offset" in dma_write
