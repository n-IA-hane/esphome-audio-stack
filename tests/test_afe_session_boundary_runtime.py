"""Exercise current AFE admission against the resolved manager's fetch lifetime.

Production adapter and GMF method bodies are compiled, not audit snapshots.
RTOS events/tasks and ESP-SR allocation/fetch are explicit host substitutes.
"""

from pathlib import Path
import os
import re
import resource
import subprocess

import pytest


ROOT = Path(os.environ.get("AUDIO_STACK_REPO", Path(__file__).resolve().parents[1]))
MANAGER = Path(os.environ.get(
    "GMF_AFE_MANAGER_SOURCE",
    ROOT / ".test-deps/esp-gmf/elements/gmf_ai_audio/src/esp_gmf_afe_manager.c",
))


def method(source, signature):
    start = source.index(signature)
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        depth += (source[index] == "{") - (source[index] == "}")
        if depth == 0:
            return source[start:index + 1]
    raise AssertionError(signature)


@pytest.fixture(scope="module")
def session_binary(tmp_path_factory):
    assert MANAGER.is_file(), "Check out the configured GMF pin in .test-deps/esp-gmf, or set GMF_AFE_MANAGER_SOURCE to its resolved manager source"
    tmp_path = tmp_path_factory.mktemp("afe-session")
    adapter = (ROOT / "esphome/components/esp_afe/esp_afe.cpp").read_text()
    header = (ROOT / "esphome/components/esp_afe/esp_afe.h").read_text()
    manager = MANAGER.read_text()
    manager_methods = "\n".join(method(manager, signature) for signature in (
        "static void fetch_task", "esp_gmf_err_t esp_gmf_afe_manager_suspend",
        "esp_gmf_err_t esp_gmf_afe_manager_set_read_cb",
        "esp_gmf_err_t esp_gmf_afe_manager_set_result_cb", "esp_gmf_err_t esp_gmf_afe_manager_destroy",
    ))
    methods = []
    names = ["set_processing_active", "start_pipeline_", "pause_pipeline_", "stop_pipeline_",
             "manager_result_cb_", "gmf_output_release_", "update_fetch_ring_free_pct_",
             "start_reconfigure_task_", "reconfigure_task_trampoline", "reconfigure_task_loop_",
             "is_reconfigure_idle", "clear_process_busy_", "setup", "set_vad_enabled_runtime_"]
    names += [name for name in ("quiesce_process_", "retire_processing_session_", "service_processing_request_")
              if f"EspAfe::{name}(" in adapter]
    for name in names:
        found = re.search(rf"\b(?:bool|void|esp_gmf_err_io_t)\s+EspAfe::{name}\(", adapter)
        assert found is not None, name
        methods.append(method(adapter, found.group(0)))
    methods.append(method(adapter, "EspAfe::~EspAfe()"))
    methods.append(method(adapter, "bool EspAfe::request_reinit_by_name(const char *name)"))
    helpers = "\n".join(method(header, f"  bool {name}(") for name in ("accepting_processing_", "wants_background_input")
                        if f"  bool {name}(" in header)
    parent = (ROOT / "esphome/components/esp_audio_stack/esp_audio_stack.cpp").read_text()
    fixture = (ROOT / "tests/fixtures/afe_session_runtime.cpp").read_text()
    fixture = fixture.replace("// MANAGER_METHODS", manager_methods).replace("// ADAPTER_HELPERS", helpers)
    fixture = fixture.replace("// ADAPTER_METHODS", "\n".join(methods))
    fixture = fixture.replace("// PARENT_METHODS", method(parent, "void ESPAudioStack::sync_processor_background_consumer_()"))
    cpp = tmp_path / "session.cpp"
    cpp.write_text(fixture)
    binary = tmp_path / "session"
    result = subprocess.run(
        ["g++", "-std=c++17", "-O1", "-g", "-pthread", "-fsanitize=address,undefined",
         str(cpp), "-o", str(binary)], capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return binary


@pytest.mark.parametrize("scenario", range(10), ids=[
    "old-fetch-after-reacquisition", "cancel-during-retirement", "persistent-consumer",
    "failed-rebuild-explicit-retry", "mode-request-during-process-drain",
    "drain-timeout-preserves-owner", "stop-while-idle", "worker-allocation-failure",
    "acquisition-request-before-setup", "repeated-consumer-edges-during-retirement",
])
def test_afe_acquisition_retires_old_fetch_before_reopening(session_binary, scenario):
    result = subprocess.run(
        [str(session_binary), str(scenario)], capture_output=True, text=True, timeout=15,
        preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_CORE, (0, 0)),
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("scenario", range(10, 16), ids=[
    "psram-owner-stack", "no-psram-internal-stack", "psram-allocation-fallback",
    "psram-task-create-fallback", "both-stack-regions-exhausted", "worker-retry-after-exhaustion",
])
def test_afe_worker_stack_placement_and_lifetime(session_binary, scenario):
    result = subprocess.run(
        [str(session_binary), str(scenario)], capture_output=True, text=True, timeout=15,
        preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_CORE, (0, 0)),
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("scenario", range(16, 21), ids=[
    "vad-only-after-retirement", "vad-during-new-acquisition", "vad-live-with-another-consumer",
    "vad-intent-without-background-policy", "vad-start-cancel-during-retirement",
])
def test_vad_intent_restarts_only_the_owned_background_path(session_binary, scenario):
    result = subprocess.run(
        [str(session_binary), str(scenario)], capture_output=True, text=True, timeout=15,
        preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_CORE, (0, 0)),
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("static_allocation", [0, 1], ids=["dynamic-only-idf", "external-stack-disabled"])
def test_afe_worker_keeps_internal_stack_when_idf_disallows_external(session_binary, tmp_path, static_allocation):
    binary = tmp_path / "internal_only"
    result = subprocess.run(
        ["g++", "-std=c++17", "-O1", "-g", "-pthread", "-fsanitize=address,undefined",
         "-DCONFIG_FREERTOS_TASK_CREATE_ALLOW_EXT_MEM=0",
         f"-DconfigSUPPORT_STATIC_ALLOCATION={static_allocation}",
         str(session_binary.parent / "session.cpp"), "-o", str(binary)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    result = subprocess.run(
        [str(binary), "10"], capture_output=True, text=True, timeout=15,
        preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_CORE, (0, 0)),
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_vad_getter_and_telemetry_reject_closed_or_replaced_epoch(tmp_path):
    header = (ROOT / "esphome/components/esp_afe/esp_afe.h").read_text()
    adapter = (ROOT / "esphome/components/esp_afe/esp_afe.cpp").read_text()
    telemetry = method(adapter, "ProcessorTelemetry EspAfe::telemetry() const")
    assignment = next(line for line in telemetry.splitlines() if "t.voice_present =" in line)
    cpp = tmp_path / "vad_epoch.cpp"
    cpp.write_text("""
#include <atomic>
#include <cassert>
#include <cstdint>
struct EspAfe {
 std::atomic<uint32_t> processing_request_{1},manager_epoch_{0};
 std::atomic<bool> processing_active_{true},voice_present_{true};
""" + method(header, "  bool accepting_processing_() const")
        + method(header, "  bool is_voice_present() const") + """
 bool telemetry_voice()const {struct {bool voice_present;} t;
""" + assignment + """
  return t.voice_present;
 }
};
int main(){
 EspAfe afe;
 assert(afe.is_voice_present()&&afe.telemetry_voice());
 // A result which entered before closing may finish updating its raw VAD.
 // Neither public path may expose that value for a closed or newer epoch.
 afe.processing_request_=2;
 assert(!afe.is_voice_present()&&!afe.telemetry_voice());
 afe.processing_request_=3;
 assert(!afe.is_voice_present()&&!afe.telemetry_voice());
 afe.manager_epoch_=1;afe.processing_active_=false;
 assert(!afe.is_voice_present()&&!afe.telemetry_voice());
 afe.processing_active_=true;
 assert(afe.is_voice_present()&&afe.telemetry_voice());
 afe.voice_present_=false;
 assert(!afe.is_voice_present()&&!afe.telemetry_voice());
}
""")
    binary = tmp_path / "vad_epoch"
    subprocess.run(["g++", "-std=c++17", "-O2", str(cpp), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
