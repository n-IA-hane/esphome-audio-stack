"""Execute manager start/pause/stop against callback ownership and failure invariants."""

from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_manager_callbacks_are_installed_before_resume_and_detached_before_reset(
    tmp_path,
):
    source = (ROOT / "esphome/components/esp_afe/esp_afe.cpp").read_text()
    methods = ""
    for name, result in (
        ("start_pipeline_", "bool"),
        ("pause_pipeline_", "bool"),
        ("stop_pipeline_", "void"),
    ):
        start = source.index(f"{result} EspAfe::{name}()")
        methods += source[start : source.index("\n}\n", start) + 2] + "\n"
    harness = r"""
#include <atomic>
#include <cassert>
#define USE_ESP_AFE_GMF_PATH
#define ESP_LOGW(...)
using esp_gmf_err_t=int;constexpr int ESP_GMF_ERR_OK=0;
using Read=int(*)(void*,int,void*,unsigned);using Result=void(*)(void*,void*);
struct Manager{Read read=nullptr;Result result=nullptr;bool paused=true;int starts=0;int fail=0;} manager;
int esp_gmf_afe_manager_suspend(void*,bool pause){manager.paused=pause;return 0;}
int esp_gmf_afe_manager_set_result_cb(void*,Result cb,void*){
 if(cb && manager.fail==1)return -1;manager.result=cb;return 0;
}
int esp_gmf_afe_manager_set_read_cb(void*,Read cb,void*){
 if(cb){assert(manager.result);if(manager.fail==2)return -1;++manager.starts;manager.paused=false;}
 else{assert(manager.paused);}manager.read=cb;return 0;
}
struct Ring{void reset(){assert(manager.paused && !manager.read && !manager.result);}} ring;
struct EspAfe{
 void* afe_manager_=&manager;bool afe_pipeline_running_=false,afe_pipeline_paused_=false;
 int staged_input_samples_=0;Ring* fetch_output_ring_=&ring;
 std::atomic<unsigned> feed_queue_frames_{0},fetch_queue_frames_{0};
 void drain_feed_input_ring_(){} void reset_output_prebuffer_(){} void flush_pipeline_before_stop_(){}
 static int manager_read_cb_(void*,int,void*,unsigned){return 0;}
 static void manager_result_cb_(void*,void*){}
 bool start_pipeline_();bool pause_pipeline_();void stop_pipeline_();
};
"""
    checks = r"""
int main(){
 EspAfe afe;
 for(int failure:{1,2}){
  manager.fail=failure;assert(!afe.start_pipeline_());
  assert(manager.paused && !manager.read && !manager.result && !afe.afe_pipeline_running_);
  manager.fail=0;assert(afe.start_pipeline_());
  int starts=manager.starts;assert(afe.start_pipeline_());assert(manager.starts==starts);
  assert(afe.pause_pipeline_());assert(manager.paused && !manager.read && !manager.result);
  assert(afe.start_pipeline_());assert(manager.starts==starts+1);
  afe.stop_pipeline_();afe.stop_pipeline_();
  assert(manager.paused && !manager.read && !manager.result && !afe.afe_pipeline_running_);
 }
}
"""
    cpp = tmp_path / "manager_lifecycle.cpp"
    cpp.write_text("#include <initializer_list>\n" + harness + methods + checks)
    exe = tmp_path / "manager_lifecycle"
    subprocess.run(["g++", "-std=c++17", "-O2", str(cpp), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)


def test_rebuilt_manager_reopens_only_current_requested_acquisition(tmp_path):
    """Apply features before admitting the current acquisition to callbacks.

    Resource allocation and algorithm construction precede these blocks and
    are not modeled. Failed feature acknowledgement must prevent start.
    """
    source = (ROOT / "esphome/components/esp_afe/esp_afe.cpp").read_text()
    helper_start = source.index("static esp_gmf_err_t apply_vad_intent(")
    helper = source[helper_start : source.index("\n}\n", helper_start) + 2]
    start = source.index("  const bool aec_requested =", source.index("bool EspAfe::install_instance_("))
    end = source.index("\n  return true;", start)
    admission = source[start:end]
    harness = r"""
#include <atomic>
#include <cassert>
#include <cstdint>
#include <initializer_list>
#define ESP_LOGE(...)
using esp_gmf_err_t=int;
constexpr int ESP_GMF_ERR_OK=0,ESP_GMF_ERR_FAIL=-2,ESP_AFE_FEATURE_AEC=1,ESP_AFE_FEATURE_VAD=2;
struct Manager{bool aec=false,vad=false,ignore_vad=false;int fail_feature=0,query_result=0;};
using esp_gmf_afe_manager_handle_t=Manager*;
struct esp_gmf_afe_manager_features_t{bool vad;};
int esp_gmf_afe_manager_get_features(Manager*m,esp_gmf_afe_manager_features_t*f){
 f->vad=m->vad;return m->query_result;
}
int esp_gmf_afe_manager_enable_features(Manager*m,int feature,bool enabled){
 if(m->fail_feature==feature)return -1;
 if(feature==ESP_AFE_FEATURE_AEC){m->aec=enabled;return static_cast<int>(enabled);}
 assert(feature==ESP_AFE_FEATURE_VAD);if(!m->ignore_vad)m->vad=enabled;
 return 0; // GMF returns reset_vad's result, independently of toggle acknowledgement.
}
// PRODUCTION_VAD_HELPER
struct EspAfe {
 std::atomic<uint32_t> processing_request_{0},manager_epoch_{0};
 std::atomic<bool> processing_active_{false},aec_enabled_{true},vad_enabled_{true};
 Manager manager;Manager*afe_manager_=&manager;
 bool allow_start=true;unsigned starts=0,cleanups=0;
 bool start_pipeline_(){
  assert(manager.aec==aec_enabled_.load()&&manager.vad==vad_enabled_.load());
  ++starts;return allow_start;
 }
 bool install(){
  auto cleanup_failed_install=[this]{++cleanups;return false;};
"""
    checks = r"""
  return true;
 }
};
int main(){
 EspAfe afe;
 assert(afe.install()&&afe.starts==0&&!afe.processing_active_);
 // All-off or a failed acquisition can leave admission false while a
 // consumer still owns an intent. A successful fresh install resumes it.
 afe.processing_request_=3;afe.manager_epoch_=1;
 assert(afe.install()&&afe.starts==1&&afe.processing_active_);
 afe.processing_request_=4;
 assert(afe.install()&&afe.starts==1&&!afe.processing_active_);
 afe.processing_request_=5;
 assert(afe.install()&&afe.starts==1&&!afe.processing_active_);
 afe.manager_epoch_=2;afe.allow_start=false;
 assert(!afe.install()&&afe.starts==2&&!afe.processing_active_&&afe.cleanups==1);
 afe.allow_start=true;
 assert(afe.install()&&afe.starts==3&&afe.processing_active_);
 for(bool aec:{false,true})for(bool vad:{false,true}){
  afe.aec_enabled_=aec;afe.vad_enabled_=vad;
  assert(afe.install()&&afe.processing_active_);
  assert(afe.manager.aec==aec&&afe.manager.vad==vad);
 }
 for(int failure:{1,2,3,4}){
  EspAfe failed;failed.processing_request_=1;
  if(failure<=2)failed.manager.fail_feature=failure;
  if(failure==3)failed.manager.ignore_vad=true;
  if(failure==4)failed.manager.query_result=-1;
  assert(!failed.install()&&failed.starts==0&&failed.cleanups==1);
 }
}
"""
    cpp = tmp_path / "install_admission.cpp"
    cpp.write_text(harness.replace("// PRODUCTION_VAD_HELPER", helper) + admission + checks)
    binary = tmp_path / "install_admission"
    subprocess.run(["g++", "-std=c++17", "-O2", str(cpp), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
