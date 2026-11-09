"""The real I2S reader returns without a clock and retains partial PCM."""
from pathlib import Path
import subprocess

import pytest
from test_volume_reference_runtime import method

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("scenario", ["clock", "partial", "stop"])
def test_read_deadline_partial_accumulation_and_stop(tmp_path, scenario):
    pipeline = (ROOT / "esphome/components/esp_audio_stack/audio_pipeline.cpp").read_text()
    code = r'''
#include <algorithm>
#include <atomic>
#include <cassert>
#include <cstdint>
#include <cstring>
#include <string>
#define ESP_LOGE(...) ((void)0)
#define LOG_W_THROTTLED(...) ((void)0)
using esp_err_t=int;
constexpr int ESP_OK=0,ESP_ERR_TIMEOUT=1,ESP_ERR_INVALID_STATE=2;
constexpr uint32_t portMAX_DELAY=UINT32_MAX;
const char*esp_err_to_name(int){return "mock";}
bool clock_present=false,stop_during_read=false;size_t offered=0,cursor=0;unsigned calls=0;
std::atomic<bool>*running=nullptr;
int i2s_channel_read(void*,void*out,size_t capacity,size_t*bytes,uint32_t timeout_ms){
 ++calls;
 assert(timeout_ms>0&&timeout_ms<=16&&"missing clock must return within one 256/16k audio period");
 if(stop_during_read)*running=false;
 *bytes=clock_present?std::min(offered,capacity):0;
 for(size_t i=0;i<*bytes;i++)static_cast<uint8_t*>(out)[i]=uint8_t(cursor++);
 return *bytes==capacity?ESP_OK:ESP_ERR_TIMEOUT;
}
struct AudioTaskCtx {
 int16_t storage[256]{},*rx_buffer=storage;size_t rx_frame_bytes=512,bus_frame_size=256,rx_pending_bytes=0;
 int consecutive_rx_errors=0,invalid_rx_state_errors=0;
};
struct ESPAudioStack {
 void*rx_handle_=this;uint32_t sample_rate_=16000;
 std::atomic<bool>audio_stack_running_{true},has_i2s_error_{false};
 bool read_rx_frame_(AudioTaskCtx&,const char*);
};
''' + method(pipeline, "bool ESPAudioStack::read_rx_frame_(") + r'''
int main(int argc,char**argv){
 ESPAudioStack s;AudioTaskCtx c;running=&s.audio_stack_running_;std::string scenario=argv[1];
 if(scenario=="clock"){
  for(unsigned i=0;i<101;i++)assert(!s.read_rx_frame_(c,"no clock"));
  assert(!s.audio_stack_running_&&s.has_i2s_error_&&calls==101);
 }else if(scenario=="partial"){
  clock_present=true;offered=128;
  for(unsigned i=0;i<3;i++)assert(!s.read_rx_frame_(c,"partial"));
  assert(s.read_rx_frame_(c,"complete"));
  for(size_t i=0;i<512;i++)assert(reinterpret_cast<uint8_t*>(c.storage)[i]==uint8_t(i));
  assert(c.rx_pending_bytes==0&&c.consecutive_rx_errors==0&&s.audio_stack_running_);
  offered=512;assert(s.read_rx_frame_(c,"next"));
 }else{
  stop_during_read=true;assert(!s.read_rx_frame_(c,"stop"));
  assert(!s.audio_stack_running_&&calls==1&&!s.has_i2s_error_);
 }
}
'''
    cpp = tmp_path / "deadline.cpp"
    cpp.write_text(code)
    binary = tmp_path / "deadline"
    built = subprocess.run(["g++", "-std=c++17", "-O1", "-g", "-fsanitize=address,undefined",
                            str(cpp), "-o", str(binary)], capture_output=True, text=True)
    assert built.returncode == 0, built.stderr
    run = subprocess.run([str(binary), scenario], capture_output=True, text=True, timeout=5)
    assert run.returncode == 0, run.stdout + run.stderr
