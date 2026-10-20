"""Execute the production manager callbacks with borrowed PCM and VAD results."""

from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_manager_result_delivers_pcm_without_an_input_job(tmp_path):
    source = (ROOT / "esphome/components/esp_afe/esp_afe.cpp").read_text()
    methods = ""
    for signature in (
        "int32_t EspAfe::manager_read_cb_",
        "void EspAfe::manager_result_cb_",
    ):
        start = source.index(signature)
        methods += source[start : source.index("\n}\n", start) + 2] + "\n"
    harness = r"""
#include <atomic>
#include <cassert>
#include <cstdint>
#include <cstring>
#include <vector>
constexpr int ESP_OK=0,ESP_FAIL=-1,ESP_GMF_IO_OK=0,VAD_SPEECH=1;
struct esp_gmf_payload_t{uint8_t*buf;uint32_t buf_length;uint32_t valid_size;};
struct afe_fetch_result_t{int16_t*data;int data_size;int vad_state;int ret_value=ESP_OK;};
struct EspAfe{
 std::atomic<bool>processing_active_{true},drain_request_{false},vad_enabled_{true},voice_present_{false};
 int input_result=0;std::vector<uint8_t>output;
 int gmf_input_acquire_(esp_gmf_payload_t*p,uint32_t n,int){
  if(input_result)return input_result;memset(p->buf,42,n);p->valid_size=n;return 0;
 }
 int gmf_output_release_(esp_gmf_payload_t*p,int){output.insert(output.end(),p->buf,p->buf+p->valid_size);return 0;}
 static int32_t manager_read_cb_(void*,int,void*,uint32_t);
 static void manager_result_cb_(afe_fetch_result_t*,void*);
};
"""
    checks = r"""
int main(){
 EspAfe afe;int16_t pcm[]={1,-42,32767,-32768};afe_fetch_result_t r{pcm,sizeof(pcm),VAD_SPEECH,1};
 // ESP-SR documents ESP_FAIL as the error sentinel, not ESP_OK as the sole valid result.
 // No input/GMF processing job needs to advance to deliver an already-ready result.
 EspAfe::manager_result_cb_(&r,&afe);
 assert(afe.voice_present_ && afe.output.size()==sizeof(pcm));assert(memcmp(afe.output.data(),pcm,sizeof(pcm))==0);
 r.ret_value=ESP_OK;afe.vad_enabled_=false;EspAfe::manager_result_cb_(&r,&afe);assert(!afe.voice_present_ && afe.output.size()==2*sizeof(pcm));
 afe.drain_request_=true;EspAfe::manager_result_cb_(&r,&afe);assert(afe.output.size()==2*sizeof(pcm));
 afe.drain_request_=false;r.ret_value=-1;EspAfe::manager_result_cb_(&r,&afe);assert(afe.output.size()==2*sizeof(pcm));
 r.ret_value=ESP_OK;r.data_size=3;EspAfe::manager_result_cb_(&r,&afe);assert(afe.output.size()==2*sizeof(pcm));
 r.data_size=sizeof(pcm);
 afe.processing_active_=false;EspAfe::manager_result_cb_(&r,&afe);assert(afe.output.size()==2*sizeof(pcm));
 EspAfe::manager_result_cb_(nullptr,&afe);r.data=nullptr;EspAfe::manager_result_cb_(&r,&afe);assert(afe.output.size()==2*sizeof(pcm));
 uint8_t input[12]{};assert(EspAfe::manager_read_cb_(input,sizeof(input),&afe,1)==12);for(auto b:input)assert(b==42);
 afe.input_result=-2;assert(EspAfe::manager_read_cb_(input,sizeof(input),&afe,1)==0);
 assert(EspAfe::manager_read_cb_(nullptr,12,&afe,1)==0);
}
"""
    cpp = tmp_path / "manager_callbacks.cpp"
    cpp.write_text(harness + methods + checks)
    exe = tmp_path / "manager_callbacks"
    subprocess.run(["g++", "-std=c++17", "-O2", str(cpp), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)


def test_stop_waits_for_borrowed_output_callback_before_reset(tmp_path):
    """Host threads model the official manager's callback mutex, not FreeRTOS."""
    source = (ROOT / "esphome/components/esp_afe/esp_afe.cpp").read_text()
    methods = ""
    for signature in (
        "void EspAfe::manager_result_cb_", "bool EspAfe::pause_pipeline_()",
        "void EspAfe::stop_pipeline_()",
    ):
        start = source.index(signature)
        methods += source[start:source.index("\n}\n", start) + 2] + "\n"
    harness = r'''
#include <atomic>
#include <cassert>
#include <condition_variable>
#include <cstdint>
#include <mutex>
#include <thread>
#include <vector>
#define USE_ESP_AFE_GMF_PATH
#define ESP_LOGW(...)
constexpr int ESP_OK=0,ESP_FAIL=-1,ESP_GMF_ERR_OK=0,VAD_SPEECH=1;
using esp_gmf_err_t=int;
struct afe_fetch_result_t{int16_t*data;int data_size;int vad_state;int ret_value=ESP_OK;};
struct esp_gmf_payload_t{uint8_t*buf;uint32_t buf_length;uint32_t valid_size;};
using Result=void(*)(afe_fetch_result_t*,void*);
std::mutex callback_mutex,observation_mutex;
std::condition_variable observed;
bool output_entered=false,allow_output=false,detach_waiting=false;
std::atomic<bool> paused{false},read_attached{true},result_attached{true};
std::atomic<int> resets{0};
int esp_gmf_afe_manager_suspend(void*,bool value){paused=value;return 0;}
int esp_gmf_afe_manager_set_read_cb(void*,void*,void*){assert(paused);read_attached=false;return 0;}
int esp_gmf_afe_manager_set_result_cb(void*,Result,void*){
 {std::lock_guard<std::mutex> lock(observation_mutex);detach_waiting=true;}observed.notify_all();
 std::lock_guard<std::mutex> lock(callback_mutex);result_attached=false;return 0;
}
struct Ring{void reset(){assert(!result_attached && !read_attached && paused);++resets;}}ring;
struct EspAfe{
 void*afe_manager_=this;bool afe_pipeline_running_=true,afe_pipeline_paused_=false;
 Ring*fetch_output_ring_=&ring;
 std::atomic<unsigned>feed_queue_frames_{0},fetch_queue_frames_{0};
 std::atomic<bool>processing_active_{true},drain_request_{false},vad_enabled_{true},voice_present_{false};
 std::vector<uint8_t>output;
 int gmf_output_release_(esp_gmf_payload_t*p,int){
  std::unique_lock<std::mutex> lock(observation_mutex);output_entered=true;observed.notify_all();
  observed.wait(lock,[]{return allow_output;});
  output.assign(p->buf,p->buf+p->valid_size);return 0;
 }
 void flush_pipeline_before_stop_(){assert(paused);}
 void drain_feed_input_ring_(){assert(!read_attached && !result_attached);}
 void reset_output_prebuffer_(){}
 static void manager_result_cb_(afe_fetch_result_t*,void*);
 bool pause_pipeline_();void stop_pipeline_();
};
'''
    checks = r'''
int main(){
 EspAfe afe;int16_t pcm[]={17,-22,32767,-32768};afe_fetch_result_t result{pcm,sizeof(pcm),VAD_SPEECH};
 std::thread callback([&]{std::lock_guard<std::mutex> lock(callback_mutex);EspAfe::manager_result_cb_(&result,&afe);});
 {std::unique_lock<std::mutex> lock(observation_mutex);observed.wait(lock,[]{return output_entered;});}
 afe.processing_active_=false;
 std::thread stop([&]{afe.stop_pipeline_();});
 {std::unique_lock<std::mutex> lock(observation_mutex);observed.wait(lock,[]{return detach_waiting;});
  assert(resets==0 && result_attached);allow_output=true;}observed.notify_all();
 callback.join();stop.join();
 assert(resets==1 && !afe.afe_pipeline_running_ && !result_attached && !read_attached);
 assert(afe.output.size()==sizeof(pcm));
 afe.stop_pipeline_();assert(resets==2);
}
'''
    cpp = tmp_path / "callback_lifetime.cpp"
    cpp.write_text(harness + methods + checks)
    exe = tmp_path / "callback_lifetime"
    subprocess.run(["g++", "-std=c++17", "-O2", "-pthread", str(cpp), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True, timeout=5)
