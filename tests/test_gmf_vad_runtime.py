"""VAD controls use the manager directly and commit state only after success."""

from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_vad_control_preserves_state_on_failure(tmp_path):
    source = (ROOT / "esphome/components/esp_afe/esp_afe.cpp").read_text()
    start = source.index("bool EspAfe::set_vad_enabled_runtime_")
    method = source[start : source.index("\n}\n", start) + 2]
    harness = r"""
#include <atomic>
#include <cassert>
#define USE_ESP_AFE_GMF_PATH
#define ESP_LOGI(...)
#define ESP_LOGW(...)
constexpr int CONFIG_MUTEX_TIMEOUT=10,ESP_AFE_FEATURE_VAD=1;
bool lock_available=true,hardware_vad=true;int manager_result=0,manager_calls=0;
void xTaskNotifyGive(void*){}
namespace esp_audio_stack{
struct ScopedLock{bool acquired;ScopedLock(void*,int):acquired(lock_available){} explicit operator bool()const{return acquired;}};
}
int esp_gmf_afe_manager_enable_features(void*,int,bool enabled){
 ++manager_calls;if(manager_result>=0)hardware_vad=enabled;return manager_result;
}
struct EspAfe{
 std::atomic<bool> vad_enabled_{true},voice_present_{true},afe_stopped_{false};
 std::atomic<bool> activity_pending_{false};void* reconfigure_task_handle_=this;
 void* config_mutex_=this;void* afe_manager_=this;
 bool accepting_processing_()const{return true;}
 bool is_initialized()const{return true;} bool all_features_disabled_()const{return false;}
 bool recreate_instance_(bool){return true;} bool set_vad_enabled_runtime_(bool);
};
"""
    checks = r"""
int main(){
 EspAfe afe;
 assert(afe.set_vad_enabled_runtime_(false));assert(!hardware_vad && !afe.vad_enabled_ && !afe.voice_present_);
 lock_available=false;assert(!afe.set_vad_enabled_runtime_(true));assert(!hardware_vad && !afe.vad_enabled_);
 lock_available=true;manager_result=-1;assert(!afe.set_vad_enabled_runtime_(true));assert(!hardware_vad && !afe.vad_enabled_);
 manager_result=0;assert(afe.set_vad_enabled_runtime_(true));assert(hardware_vad && afe.vad_enabled_);
 int calls=manager_calls;assert(afe.set_vad_enabled_runtime_(true));assert(manager_calls==calls);
}
"""
    cpp = tmp_path / "vad.cpp"
    cpp.write_text(harness + method + checks)
    exe = tmp_path / "vad"
    subprocess.run(["g++", "-std=c++17", "-O2", str(cpp), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
