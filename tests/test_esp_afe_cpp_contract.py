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
    assert "rebuild-only on the ESP-SR single-mic pipeline" in header

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
    assert "const bool gmf_direct_frame = gmf_path && offset == 0 && stage_samples == fs;" in process
    assert "stage_afe_input_frame(static_cast<int16_t *>(gmf_slot)" in process


def test_gmf_output_bridge_preserves_frame_boundaries_and_optional_reserve() -> None:
    init = read("__init__.py")
    cpp = read("esp_afe.cpp")
    header = read("esp_afe.h")
    ring = (
        ROOT
        / "esphome"
        / "components"
        / "esp_audio_stack"
        / "audio_core_ring_buffer_caps.h"
    ).read_text(encoding="utf-8")

    assert 'CONF_OUTPUT_PREBUFFER_FRAMES = "output_prebuffer_frames"' in init
    assert "cv.Optional(CONF_OUTPUT_PREBUFFER_FRAMES, default=0)" in init
    assert "output_prebuffer_frames requires mic_num: 2" in init
    assert "set_output_prebuffer_frames" in header

    process = cpp[cpp.index("bool EspAfe::process(") : cpp.index("\nbool EspAfe::reinit_by_name")]
    assert "this->fetch_output_ring_->available() / output_bytes" in process
    assert "this->fetch_output_ring_->available() >= output_bytes" in process
    assert "static_cast<size_t>(this->output_prebuffer_frames_) + 1U" in process
    assert "this->fetch_output_ring_->read(reinterpret_cast<uint8_t *>(out), output_bytes, 0)" in process

    output_start = cpp.index("esp_gmf_err_io_t EspAfe::gmf_output_release_(")
    output = cpp[output_start : cpp.index("\n#endif", output_start)]
    assert "write_without_replacement(load->buf, load->valid_size, 0, false)" in output
    assert "output size is not frame-aligned" not in output
    assert "size_t nosplit_items_waiting() const;" in ring
    assert "vRingbufferGetInfo(this->handle_" in ring


def test_dual_mic_agc_runs_after_complete_gmf_frame_assembly() -> None:
    cpp = read("esp_afe.cpp")
    header = read("esp_afe.h")

    assert "esp_agc_open(AGC_MODE_2, 16000)" in cpp
    assert "set_agc_config(this->post_afe_agc_" in cpp
    assert "esp_agc_process(this->post_afe_agc_" in cpp
    assert "kPostAfeAgcQuantumSamples = 160" in header
    assert "latency=10ms" in cpp

    config = cpp[cpp.index("const bool use_post_afe_agc") : cpp.index("cfg->afe_perferred_core")]
    assert "afe_mic_channels >= 2" in config
    assert "&& !use_post_afe_agc" in config

    process = cpp[cpp.index("bool EspAfe::process(") : cpp.index("\nbool EspAfe::reinit_by_name")]
    read_pos = process.index("fetch_output_ring_->read")
    complete = process.index("if (got == output_bytes)", read_pos)
    agc = process.index("process_post_afe_agc_frame_", complete)
    assert read_pos < complete < agc

    output_start = cpp.index("esp_gmf_err_io_t EspAfe::gmf_output_release_(")
    output = cpp[output_start : cpp.index("\n#endif", output_start)]
    assert "process_post_afe_agc_frame_" not in output
    assert "write_without_replacement(load->buf, load->valid_size" in output


def test_gmf_initial_vad_off_is_applied_only_after_element_open() -> None:
    cpp = read("esp_afe.cpp")
    header = read("esp_afe.h")

    assert "cfg->vad_init = true;" in cpp
    assert "std::atomic<bool> gmf_vad_state_pending_{false};" in header

    start = cpp[cpp.index("bool EspAfe::start_pipeline_()") : cpp.index("\nbool EspAfe::pause_pipeline_()")]
    assert start.index("ESP_AFE_FEATURE_VAD, true") < start.index("esp_gmf_pipeline_run")
    assert "gmf_vad_state_pending_.store(!this->vad_enabled_" in start

    output_start = cpp.index("esp_gmf_err_io_t EspAfe::gmf_output_release_(")
    output = cpp[output_start : cpp.index("\n#endif", output_start)]
    assert "apply_pending_gmf_vad_state_();" in output

    apply_start = cpp.index("bool EspAfe::apply_pending_gmf_vad_state_()")
    apply = cpp[apply_start : cpp.index("\nvoid EspAfe::gmf_event_cb_", apply_start)]
    assert "ESP_AFE_FEATURE_VAD, false" in apply
    assert "Initial GMF VAD state applied: OFF" in apply

    callback_start = cpp.index("void EspAfe::gmf_event_cb_(")
    callback = cpp[callback_start : cpp.index("\n#endif", callback_start)]
    assert callback.index("!self->vad_enabled_.load") < callback.index("GMF AFE voice transition")


def test_esp_afe_uses_current_espressif_afe_dependencies() -> None:
    init = read("__init__.py")
    aec_init = read_aec("__init__.py")

    assert 'add_idf_component(name="espressif/esp-sr", ref="^2.5.3")' in init
    assert 'name="espressif/gmf_ai_audio"' in init
    assert 'repo="https://github.com/n-IA-hane/esp-gmf.git"' in init
    assert 'ref="43b1e18f2a9234393a65d4b7eba2f132b95a5a24"' in init
    assert 'path="elements/gmf_ai_audio"' in init
    assert not (AFE / "idf_components" / "gmf_ai_audio").exists()
    assert 'ref="0.8.3"' not in init
    assert 'add_idf_component(name="espressif/esp-sr", ref="^2.5.3")' in aec_init
    assert 'ref="^2.4.4"' not in aec_init


def test_afe_rebuild_timeout_never_destroys_a_busy_instance() -> None:
    cpp = read("esp_afe.cpp")
    rebuild = cpp[
        cpp.index("bool EspAfe::recreate_instance_(") :
        cpp.index("\nbool EspAfe::", cpp.index("bool EspAfe::recreate_instance_(") + 1)
    ]

    timeout = rebuild.index("Drain timeout waiting for process() to quiesce")
    abort = rebuild.index("if (!drained)")
    detach = rebuild.index("AfeInstance old = this->detach_instance_()")
    assert timeout < abort < detach
    assert "this->drain_request_.store(false, std::memory_order_seq_cst);" in rebuild[abort:detach]
    assert "return false;" in rebuild[abort:detach]
    wait = rebuild[rebuild.index("this->process_drain_waiter_.store(waiter") : timeout]
    assert wait.count("this->process_busy_.load(std::memory_order_seq_cst)") >= 2


def test_rebuild_state_is_published_without_racing_live_afe_fields() -> None:
    cpp = read("esp_afe.cpp")
    header = read("esp_afe.h")

    assert "std::atomic<bool> instance_ready_{false};" in header
    assert "std::atomic<int> last_spec_process_size_{0};" in header
    assert "std::atomic<int> last_spec_fetch_size_{0};" in header
    assert "return this->instance_ready_.load(std::memory_order_acquire);" in header

    frame_spec = cpp[cpp.index("FrameSpec EspAfe::frame_spec() const") : cpp.index("\nFeatureControl EspAfe::")]
    assert "process_chunksize_" not in frame_spec
    assert "fetch_chunksize_" not in frame_spec
    assert frame_spec.count(".load(std::memory_order_acquire)") == 3

    process = cpp[cpp.index("bool EspAfe::process(") : cpp.index("\nbool EspAfe::reinit_by_name")]
    busy = process.index("this->process_busy_.store(true, std::memory_order_seq_cst);")
    drain = process.index("this->drain_request_.load(std::memory_order_seq_cst)")
    first_live_size = process.index("int qs = this->process_chunksize_")
    assert busy < drain < first_live_size

    setter = cpp[cpp.index("void EspAfe::set_processing_active") : cpp.index("\n#ifdef USE_ESP_AFE_GMF_PATH", cpp.index("void EspAfe::set_processing_active"))]
    assert setter.index("ScopedLock lock") < setter.index("this->start_pipeline_()")

    release = cpp[cpp.index("void EspAfe::release_runtime_buffers_()") : cpp.index("\nEspAfe::~EspAfe()")]
    assert "this->direct_feed_signal_ = nullptr;" not in release
