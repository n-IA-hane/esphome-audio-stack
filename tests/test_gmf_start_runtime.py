"""A cold GMF start must let the element attach callbacks before worker wakeup."""

from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_cold_start_waits_for_gmf_callback_owner(tmp_path):
    source = (ROOT / "esphome/components/esp_afe/esp_afe.cpp").read_text()
    start = source.index("bool EspAfe::start_pipeline_()")
    stop = source.index("\nbool EspAfe::pause_pipeline_()", start)
    harness = r"""
#include <atomic>
#include <cassert>
#define USE_ESP_AFE_GMF_PATH
#define ESP_LOGW(...)
using esp_gmf_err_t=int;constexpr int ESP_GMF_ERR_OK=0,ESP_AFE_FEATURE_VAD=1;
bool opened=false;int resumes=0,runs=0,run_result=0;
int esp_gmf_pipeline_run(void*){++runs;return run_result;}
int esp_gmf_pipeline_resume(void*){assert(opened);return 0;}
void esp_gmf_afe_manager_suspend(void*,bool pause){if(!pause){assert(opened);++resumes;}}
int esp_gmf_afe_manager_enable_features(void*,int feature,bool enabled){
 assert(feature==ESP_AFE_FEATURE_VAD && enabled);return 0;
}
struct Ring{void reset(){}} ring;
struct EspAfe {
 void *afe_pipeline_=&ring,*afe_manager_=&ring;
 bool afe_pipeline_running_=false,afe_pipeline_paused_=false;
 int staged_input_samples_=0;Ring* fetch_output_ring_=&ring;
 std::atomic<unsigned> feed_queue_frames_{0},fetch_queue_frames_{0};
 std::atomic<bool> vad_enabled_{false},gmf_vad_state_pending_{false};
 void drain_feed_input_ring_(){} void reset_output_prebuffer_(){} void reset_post_afe_agc_(){}
 bool start_pipeline_();
};
"""
    checks = r"""
int main(){
 EspAfe afe;
 assert(afe.start_pipeline_());
 assert(runs==1 && resumes==0 && afe.afe_pipeline_running_);
 // The actual element's set_read_cb owns this transition after asynchronous open.
 opened=true;esp_gmf_afe_manager_suspend(afe.afe_manager_,false);
 afe.afe_pipeline_running_=false;afe.afe_pipeline_paused_=true;
 assert(afe.start_pipeline_());assert(resumes==2 && runs==1);
 afe.afe_pipeline_running_=false;afe.afe_pipeline_paused_=false;opened=false;run_result=-1;
 assert(!afe.start_pipeline_());assert(resumes==2 && !afe.afe_pipeline_running_);
}
"""
    cpp = tmp_path / "start.cpp"
    cpp.write_text(harness + source[start:stop] + checks)
    exe = tmp_path / "start"
    subprocess.run(["g++", "-std=c++17", "-O2", str(cpp), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
