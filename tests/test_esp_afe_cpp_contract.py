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
    assert 'ref="43b1e18f2a9234393a65d4b7eba2f132b95a5a24"' in init
    assert 'path="elements/gmf_ai_audio"' in init
    assert not (AFE / "idf_components" / "gmf_ai_audio").exists()
    assert 'ref="0.8.3"' not in init
    assert 'add_idf_component(name="espressif/esp-sr", ref="^2.4.6")' in aec_init
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


def test_direct_fetch_worker_is_persistent_event_driven_and_atomic() -> None:
    cpp = read("esp_afe.cpp")
    header = read("esp_afe.h")
    worker = cpp[
        cpp.index("void EspAfe::direct_fetch_task_loop_()") :
        cpp.index("\nbool EspAfe::prepare_direct_fetch_task_()")
    ]

    assert "while (true)" in worker
    assert "ulTaskNotifyTake(pdTRUE, portMAX_DELAY);" in worker
    assert "xSemaphoreTake(this->direct_feed_signal_, portMAX_DELAY)" in worker
    assert "xSemaphoreTake(this->direct_feed_signal_, fetch_timeout)" not in worker
    assert "direct_fetch_running_.load(std::memory_order_acquire)" in worker
    assert "std::atomic<bool> direct_fetch_running_{false};" in header
    assert "std::atomic<bool> direct_fetch_quiesced_{true};" in header
    assert "vTaskDelay(" not in cpp
    # A finite timeout is retained only for ESP-SR fetch_with_delay() after a
    # real feed event; it is a fault watchdog, never the worker cadence.
    assert "fetch_with_delay(this->direct_data_, fetch_timeout)" in worker
    stop = cpp[cpp.index("bool EspAfe::stop_direct_fetch_task_()") : cpp.index("\nvoid EspAfe::destroy_direct_fetch_task_()")]
    assert stop.count("direct_fetch_quiesced_.load(std::memory_order_acquire)") >= 3


def test_direct_fetch_stop_before_first_run_still_publishes_quiescence() -> None:
    cpp = read("esp_afe.cpp")
    worker = cpp[
        cpp.index("void EspAfe::direct_fetch_task_loop_()") :
        cpp.index("\nbool EspAfe::prepare_direct_fetch_task_()")
    ]

    wake = worker.index("ulTaskNotifyTake(pdTRUE, portMAX_DELAY);")
    inner = worker.index("while (this->direct_fetch_running_.load", wake)
    assert "continue;" not in worker[wake:inner]


def test_direct_fetch_static_stack_uses_esp_idf_byte_depth() -> None:
    cpp = read("esp_afe.cpp")
    header = read("esp_afe.h")

    assert "kDirectFetchTaskStackBytes = 4096;" in header
    assert "(kDirectFetchTaskStackBytes + sizeof(StackType_t) - 1) / sizeof(StackType_t)" in header
    assert "heap_caps_malloc(kDirectFetchTaskStackWords * sizeof(StackType_t)" in cpp
    assert '"afe_fetch", kDirectFetchTaskStackBytes,' in cpp
    assert "const int core = this->fetch_task_core_;" in cpp
    assert "const int prio = this->fetch_task_priority_;" in cpp
    prepare = cpp[
        cpp.index("bool EspAfe::prepare_direct_fetch_task_()") :
        cpp.index("\nbool EspAfe::start_direct_fetch_task_()")
    ]
    failure = prepare[prepare.index("if (this->direct_fetch_task_handle_ == nullptr)") :]
    assert "heap_caps_free(this->direct_fetch_task_stack_);" in failure
    assert "this->direct_fetch_task_stack_ = nullptr;" in failure


def test_persistent_direct_fetch_worker_is_destroyed_only_after_quiescing() -> None:
    cpp = read("esp_afe.cpp")
    header = read("esp_afe.h")

    destroy = cpp[
        cpp.index("void EspAfe::destroy_direct_fetch_task_()") :
        cpp.index("\n#endif", cpp.index("void EspAfe::destroy_direct_fetch_task_()"))
    ]
    assert "void destroy_direct_fetch_task_();" in header
    assert destroy.index("stop_direct_fetch_task_()") < destroy.index("vTaskDelete")
    delete = destroy.index("vTaskDelete")
    assert "heap_caps_free" in destroy[delete:]
    destructor = cpp[cpp.index("EspAfe::~EspAfe()") :]
    assert "this->destroy_direct_fetch_task_();" in destructor


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
