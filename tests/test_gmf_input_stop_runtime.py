"""Exercise the real GMF input callback's cooperative cancellation contract."""

from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def admission_helper():
    header = (ROOT / "esphome/components/esp_afe/esp_afe.h").read_text()
    start = header.index("  bool accepting_processing_() const {")
    return header[start:header.index("\n  }", start) + 4]


def test_inactive_input_aborts_instead_of_generating_pcm(tmp_path):
    s = (ROOT / "esphome/components/esp_afe/esp_afe.cpp").read_text()
    a = s.index("esp_gmf_err_io_t EspAfe::gmf_input_acquire_(")
    b = s.index("\nesp_gmf_err_io_t EspAfe::gmf_output_release_", a)
    harness = r"""
#include <atomic>
#include <algorithm>
#include <cassert>
#include <cstdint>
#include <cstring>
using TickType_t=uint32_t; using esp_gmf_err_io_t=int;
constexpr int ESP_GMF_IO_OK=0,ESP_GMF_IO_FAIL=-1,ESP_GMF_IO_TIMEOUT=-2,ESP_GMF_IO_ABORT=-3;
constexpr bool ESP_AFE_TIMING_TELEMETRY=false;
#define pdMS_TO_TICKS(x) (x)
#define ESP_LOGW(...)
struct esp_gmf_payload_t { void *buf; size_t buf_length; size_t valid_size; };
TickType_t observed_wait=0; bool provide_item=false; unsigned returned=0; uint8_t input[8]={1,2,3,4,5,6,7,8};
void *xRingbufferReceive(void*,size_t *n,TickType_t wait) {observed_wait=wait;*n=8;return provide_item?input:nullptr;}
void vRingbufferReturnItem(void*,void*) {++returned;}
int64_t esp_timer_get_time(){return 0;}
void diag_add(std::atomic<uint32_t>& c){++c;}
void decrement_if_nonzero(std::atomic<uint32_t>& c){if(c)c--;}
void update_peak_atomic(std::atomic<uint32_t>& c,uint32_t n){c=std::max(c.load(),n);}
struct EspAfe {
 std::atomic<uint32_t> processing_request_{1},manager_epoch_{0};
 // ADMISSION_HELPER
 void *feed_input_ring_=input;std::atomic<bool> processing_active_{false},drain_request_{false};int feed_chunksize_=1024;
 std::atomic<uint32_t> feed_rejected_{0},feed_queue_frames_{0},feed_us_last_{0},feed_us_max_{0},feed_ok_{0};
 esp_gmf_err_io_t gmf_input_acquire_(esp_gmf_payload_t*,uint32_t,int);
};
"""
    checks = r"""
int main(){
 EspAfe afe;uint8_t output[8]={};esp_gmf_payload_t load{output,8,8};
 assert(afe.gmf_input_acquire_(&load,8,-1)==ESP_GMF_IO_ABORT);
 assert(observed_wait==64 && load.valid_size==0);
 provide_item=true;load.valid_size=8;
 assert(afe.gmf_input_acquire_(&load,8,20)==ESP_GMF_IO_ABORT);
 assert(observed_wait==20 && load.valid_size==0 && returned==1);
 afe.processing_active_=true;
 assert(afe.gmf_input_acquire_(&load,8,20)==ESP_GMF_IO_OK);
 assert(load.valid_size==8 && std::memcmp(output,input,8)==0 && returned==2);
 provide_item=false;
 assert(afe.gmf_input_acquire_(&load,8,20)==ESP_GMF_IO_TIMEOUT);
 // A reconfigure retains active consumers but must release its input waiter.
 afe.drain_request_=true;
 assert(afe.gmf_input_acquire_(&load,8,-1)==ESP_GMF_IO_ABORT);
 assert(observed_wait==64 && load.valid_size==0);
 provide_item=true;
 assert(afe.gmf_input_acquire_(&load,8,-1)==ESP_GMF_IO_ABORT);
 assert(observed_wait==64 && returned==3 && afe.feed_ok_==1);
 afe.drain_request_=false;
 assert(afe.gmf_input_acquire_(&load,8,20)==ESP_GMF_IO_OK);
 assert(returned==4 && afe.feed_ok_==2);
}
"""
    cpp = tmp_path / "input.cpp"
    cpp.write_text(harness.replace("// ADMISSION_HELPER", admission_helper()) + s[a:b] + checks)
    exe = tmp_path / "input"
    subprocess.run(["g++", "-std=c++17", "-O2", str(cpp), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
