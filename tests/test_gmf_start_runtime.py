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
