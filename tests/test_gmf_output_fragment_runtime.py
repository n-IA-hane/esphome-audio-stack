"""Execute the production GMF output callback against a bounded host FIFO."""

from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_gmf_partial_payloads_preserve_every_sample(tmp_path):
    source = (ROOT / "esphome/components/esp_afe/esp_afe.cpp").read_text()
    start = source.index("esp_gmf_err_io_t EspAfe::gmf_output_release_(")
    callback = source[start : source.index("\n#endif", start)]
    harness = r"""
#include <atomic>
#include <algorithm>
#include <cassert>
#include <cstdint>
#include <cstring>
#include <vector>
using esp_gmf_err_io_t = int;
constexpr int ESP_GMF_IO_OK=0, ESP_GMF_IO_FAIL=-1;
using TaskHandle_t = void *;
void xTaskNotifyGive(TaskHandle_t) {}
#define ESP_LOGW(...)
struct esp_gmf_payload_t { void *buf; size_t valid_size; };
void diag_add(std::atomic<uint32_t>& n, uint32_t v=1) { n += v; }
uint32_t diag_increment_and_get(std::atomic<uint32_t>& n) { return ++n; }
void update_peak_atomic(std::atomic<uint32_t>& n, uint32_t v) { n=std::max(n.load(),v); }
struct Ring {
 std::vector<uint8_t> bytes;
 size_t available() const { return bytes.size(); }
 size_t write_without_replacement(const void *p,size_t n,int,bool) {
   if (bytes.size()+n>8192) return 0;
   const auto *b=static_cast<const uint8_t*>(p); bytes.insert(bytes.end(),b,b+n); return n;
 }
};
struct EspAfe {
 std::atomic<bool> processing_active_{true};
 int fetch_chunksize_=1024;
 Ring storage;
 Ring *fetch_output_ring_=&storage;
 std::atomic<uint32_t> fetch_timeout_{0}, output_ring_drop_{0},fetch_ok_{0},fetch_queue_frames_{0},fetch_queue_peak_{0};
 std::atomic<TaskHandle_t> pipeline_flush_waiter_{nullptr};
 void apply_pending_gmf_vad_state_() {}
 void update_fetch_ring_free_pct_() {}
 esp_gmf_err_io_t gmf_output_release_(esp_gmf_payload_t *,int);
};
"""
    checks = r"""
int main() {
 EspAfe afe;
 std::vector<uint8_t> samples(8192);
 for (size_t i=0;i<samples.size();++i) samples[i]=uint8_t(i*37+5);
 size_t offset=0;
 for (size_t n : {1024U,3072U,512U,512U,3072U}) {
   esp_gmf_payload_t payload{samples.data()+offset,n};
   assert(afe.gmf_output_release_(&payload,0)==ESP_GMF_IO_OK);
   offset+=n;
   assert(afe.storage.bytes.size()==offset);
   assert(std::equal(afe.storage.bytes.begin(),afe.storage.bytes.end(),samples.begin()));
 }
 assert(afe.fetch_ok_==4);
 esp_gmf_payload_t overflow{samples.data(),1024};
 afe.gmf_output_release_(&overflow,0);
 assert(afe.output_ring_drop_==1 && afe.storage.bytes.size()==8192);
 afe.processing_active_=false;
 afe.gmf_output_release_(&overflow,0);
 assert(afe.output_ring_drop_==1);
}
"""
    cpp = tmp_path / "callback.cpp"
    cpp.write_text(harness + callback + checks)
    exe = tmp_path / "callback"
    subprocess.run(["g++", "-std=c++17", "-O2", str(cpp), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
