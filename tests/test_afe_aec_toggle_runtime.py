"""Exercise GMF AEC control through the production feature-switch method."""

from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_aec_toggle_preserves_live_instance_and_rejects_control_failure(tmp_path):
    cpp = (ROOT / "esphome/components/esp_afe/esp_afe.cpp").read_text()
    start = cpp.index("bool EspAfe::set_aec_enabled_runtime_(")
    end = cpp.index("\nbool EspAfe::set_vad_enabled_runtime_", start)
    harness = r"""
#include <atomic>
#include <cassert>
#define USE_ESP_AFE_GMF_PATH
#define ESP_LOGW(...)
#define ESP_LOGI(...)
constexpr int CONFIG_MUTEX_TIMEOUT=1, ESP_AFE_FEATURE_AEC=2;
bool can_lock=true; int ret=0,calls=0;
namespace esp_audio_stack {
struct ScopedLock { ScopedLock(void*,int){} operator bool() const{return can_lock;} };
}
int esp_gmf_afe_manager_enable_features(void* manager,int feature,bool enabled){
 assert(manager && feature==ESP_AFE_FEATURE_AEC); ++calls;
 return ret<0 ? ret : static_cast<int>(enabled);
}
struct EspAfe {
 int mic_num_=1, rebuilds=0; bool ready=true,others_enabled=true;
 std::atomic<bool> aec_enabled_{true},afe_stopped_{false};
 void* config_mutex_=this; void* afe_manager_=this;
 bool is_initialized()const{return ready;}
 bool all_features_disabled_()const{return !others_enabled && !aec_enabled_.load();}
 bool recreate_instance_(bool){++rebuilds;afe_stopped_=all_features_disabled_();return true;}
 bool set_aec_enabled_runtime_(bool);
};
"""
    checks = r"""
int main(){
 for(int microphones: {1,2}){
  EspAfe afe;afe.mic_num_=microphones;auto* manager=afe.afe_manager_;
  for(int i=0;i<20;++i){
   assert(afe.set_aec_enabled_runtime_(false));assert(!afe.aec_enabled_);
   assert(afe.set_aec_enabled_runtime_(true));assert(afe.aec_enabled_);
   assert(afe.afe_manager_==manager && afe.rebuilds==0 && !afe.afe_stopped_);
  }
  int before=calls; assert(afe.set_aec_enabled_runtime_(true));assert(calls==before);
  ret=-1;assert(!afe.set_aec_enabled_runtime_(false));assert(afe.aec_enabled_);
  ret=0;assert(afe.set_aec_enabled_runtime_(false));assert(!afe.aec_enabled_);
  can_lock=false;assert(!afe.set_aec_enabled_runtime_(true));assert(!afe.aec_enabled_);
  can_lock=true;assert(afe.set_aec_enabled_runtime_(true));assert(afe.rebuilds==0);
  afe.others_enabled=false;assert(afe.set_aec_enabled_runtime_(false));
  assert(afe.rebuilds==1 && afe.afe_stopped_);
  assert(afe.set_aec_enabled_runtime_(true));assert(afe.rebuilds==2 && !afe.afe_stopped_);
 }
}
"""
    path = tmp_path / "toggle.cpp"
    path.write_text("#include <initializer_list>\n" + harness + cpp[start:end] + checks)
    binary = tmp_path / "toggle"
    subprocess.run(
        [
            "g++",
            "-std=c++17",
            "-Wall",
            "-Wextra",
            "-Wno-unused-variable",
            str(path),
            "-o",
            str(binary),
        ],
        check=True,
    )
    subprocess.run([str(binary)], check=True)
