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
    """Exercise the install admission block with the current consumer intent.

    Resource allocation and algorithm construction precede this block and are
    not modeled. It must also resume a still-live consumer after all-off.
    """
    source = (ROOT / "esphome/components/esp_afe/esp_afe.cpp").read_text()
    start = source.index("  const uint32_t request =", source.index("bool EspAfe::install_instance_("))
    end = source.index("\n  return true;", start)
    admission = source[start:end]
    harness = r"""
#include <atomic>
#include <cassert>
#include <cstdint>
#define ESP_LOGE(...)
struct EspAfe {
 std::atomic<uint32_t> processing_request_{0},manager_epoch_{0};
 std::atomic<bool> processing_active_{false};
 bool allow_start=true;unsigned starts=0,cleanups=0;
 bool start_pipeline_(){++starts;return allow_start;}
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
}
"""
    cpp = tmp_path / "install_admission.cpp"
    cpp.write_text(harness + admission + checks)
    binary = tmp_path / "install_admission"
    subprocess.run(["g++", "-std=c++17", "-O2", str(cpp), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
