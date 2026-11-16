"""Real RX/dispatch methods preserve the native DMA clock or report it unknown."""

from pathlib import Path
import subprocess

import esphome
import pytest

from test_volume_reference_runtime import method


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def capture_clock_binary(tmp_path_factory):
    directory = tmp_path_factory.mktemp("capture-clock")
    component = ROOT / "esphome/components/esp_audio_stack"
    pipeline = (component / "audio_pipeline.cpp").read_text()
    owner = (component / "esp_audio_stack.cpp").read_text()
    native = Path(esphome.__file__).parent / "components/i2s_audio/microphone/capture_time.h"
    (directory / "native_i2s_capture_time.h").write_bytes(native.read_bytes())
    audio_header = directory / "esphome/components/audio/audio.h"
    audio_header.parent.mkdir(parents=True)
    audio_header.write_text(
        "#pragma once\n#include <cstdint>\nnamespace esphome::audio {\n"
        "struct AudioStreamInfo {uint8_t bits,channels;uint32_t rate;"
        "AudioStreamInfo(uint8_t b,uint8_t c,uint32_t r):bits(b),channels(c),rate(r){}"
        "uint32_t bytes_to_frames(uint32_t n)const{return n/(bits/8*channels);}"
        "uint32_t get_sample_rate()const{return rate;}};}\n"
    )
    source = r'''
#include <algorithm>
#include <atomic>
#include <cassert>
#include <cstdint>
#include <cstring>
#include <string>
#include "native_i2s_capture_time.h"
#define USE_AUDIO_PROCESSOR
#define USE_ESP_AUDIO_STACK_MONO_REF
#define ESP_LOGE(...) ((void)0)
#define LOG_W_THROTTLED(...) ((void)0)
#define IRAM_ATTR
#define portENTER_CRITICAL(...) ((void)0)
#define portEXIT_CRITICAL(...) ((void)0)
#define portENTER_CRITICAL_ISR(...) ((void)0)
#define portEXIT_CRITICAL_ISR(...) ((void)0)
using i2s_chan_handle_t=void*;
struct i2s_event_data_t {size_t size;};
constexpr int ESP_OK=0,ESP_ERR_TIMEOUT=1,ESP_ERR_INVALID_STATE=2;
using esp_err_t=int;
const char*esp_err_to_name(int){return "host";}
uint64_t byte_position=0;int64_t now=1000000;size_t offered=512;bool overflow=false;
int64_t esp_timer_get_time(){return now;}
int i2s_channel_read(void*,void*,size_t,size_t*,uint32_t);
namespace esphome::esp_audio_stack {
struct AudioTaskCtx {
 int16_t storage[256]{},processed[256]{},*rx_buffer=storage,*mic_buffer=storage,*output_buffer=nullptr;
 const int16_t*processor_input=nullptr;int16_t*spk_ref_buffer=storage,*aec_output=processed;
 size_t rx_frame_bytes=512,bus_frame_size=256,rx_pending_bytes=0;
 size_t output_frame_bytes=512,output_frame_size=256,current_output_frame_size=256,current_output_frame_bytes=512;
 uint8_t i2s_bps=2;struct {uint8_t stride=1;}rx_layout;
 uint32_t ratio=1,rx_capture_frame_drops=0;
 int64_t rx_capture_time_us=0,output_capture_time_us=0;
 int consecutive_rx_errors=0,invalid_rx_state_errors=0;
 bool processor_enabled=false,processor_ready=false,mic_running=true,use_tdm_ref=false,use_stereo_aec_ref=false;
};
struct ESPAudioStack {
 void*rx_handle_=this;uint32_t sample_rate_=16000;
 std::atomic<bool>audio_stack_running_{true},has_i2s_error_{false},rx_capture_tracking_active_{true};
 std::atomic<uint32_t>rx_dma_buffers_dropped_{0};
 uint32_t rx_dma_bytes_received_=0,rx_dma_bytes_read_=0,rx_dma_descriptor_count_=4,rx_dma_buffer_bytes_=128;
 int64_t rx_dma_receive_time_us_=0,seen=-1;int rx_capture_lock_=0;unsigned processed=0;
 void update_rx_capture_time_(AudioTaskCtx&,size_t,uint32_t);
 bool read_rx_frame_(AudioTaskCtx&,const char*);
 void process_rx_path_(AudioTaskCtx&);
 bool can_dispatch_mic_frame_(const AudioTaskCtx&)const;
 void process_aec_and_callbacks_(AudioTaskCtx&);
 bool convert_rx_frame_(AudioTaskCtx&){return true;}
 void apply_input_conditioning_(AudioTaskCtx&){}
 void apply_post_processor_gain_(AudioTaskCtx&){}
 void fill_mono_aec_reference_(AudioTaskCtx&){}
 void run_processor_(AudioTaskCtx&ctx){++processed;ctx.output_buffer=ctx.aec_output;}
 void dispatch_mic_callbacks_(const AudioTaskCtx&ctx){seen=ctx.output_capture_time_us;}
 static bool rx_on_recv_callback(void*,i2s_event_data_t*,void*);
 static bool rx_on_recv_q_ovf_callback(void*,i2s_event_data_t*,void*);
};
'''
    for signature, text in (
        ("static inline bool IRAM_ATTR isr_load_flag(", owner),
        ("static inline uint32_t IRAM_ATTR isr_load_u32(", owner),
        ("bool IRAM_ATTR ESPAudioStack::rx_on_recv_callback(", owner),
        ("bool IRAM_ATTR ESPAudioStack::rx_on_recv_q_ovf_callback(", owner),
        ("void ESPAudioStack::update_rx_capture_time_(", pipeline),
        ("bool ESPAudioStack::read_rx_frame_(", pipeline),
        ("void ESPAudioStack::process_rx_path_(", pipeline),
        ("bool ESPAudioStack::can_dispatch_mic_frame_(", pipeline),
        ("void ESPAudioStack::process_aec_and_callbacks_(", pipeline),
    ):
        source += method(text, signature) + "\n"
    source += r'''
}
using namespace esphome::esp_audio_stack;
ESPAudioStack*active=nullptr;
int i2s_channel_read(void*,void*out,size_t capacity,size_t*bytes,uint32_t timeout){
 assert(timeout>0&&timeout<=16);*bytes=std::min(offered,capacity);
 byte_position+=*bytes;now=1000000+int64_t(byte_position)*1000000/32000;
 i2s_event_data_t event{*bytes};
 ESPAudioStack::rx_on_recv_callback(active->rx_handle_,&event,active);
 if(overflow){ESPAudioStack::rx_on_recv_q_ovf_callback(active->rx_handle_,&event,active);overflow=false;}
 memset(out,1,*bytes);return *bytes==capacity?ESP_OK:ESP_ERR_TIMEOUT;
}
int main(int,char**argv){
 ESPAudioStack stack;AudioTaskCtx frame;active=&stack;std::string scenario=argv[1];
 if(scenario=="partial"){
  offered=128;
  for(unsigned i=0;i<3;++i)assert(!stack.read_rx_frame_(frame,"partial"));
  assert(stack.read_rx_frame_(frame,"complete"));
  assert(frame.rx_capture_time_us==1000000&&frame.rx_pending_bytes==0);
 }else if(scenario=="overflow"){
  overflow=true;assert(stack.read_rx_frame_(frame,"drop"));assert(frame.rx_capture_time_us==0);
 }else if(scenario=="wrap"){
  stack.rx_dma_bytes_received_=UINT32_MAX-255;stack.rx_dma_bytes_read_=UINT32_MAX-255;
  assert(stack.read_rx_frame_(frame,"wrap"));assert(frame.rx_capture_time_us==1000000);
 }else if(scenario=="lifetime"){
  i2s_event_data_t event{512};
  stack.rx_capture_tracking_active_=false;
  stack.rx_on_recv_callback(stack.rx_handle_,&event,&stack);
  stack.rx_on_recv_q_ovf_callback(stack.rx_handle_,&event,&stack);
  assert(stack.rx_dma_bytes_received_==0&&stack.rx_dma_buffers_dropped_==0);
  stack.rx_capture_tracking_active_=true;
  stack.rx_on_recv_callback(nullptr,&event,&stack);
  assert(stack.rx_dma_bytes_received_==0);
  assert(stack.read_rx_frame_(frame,"next generation"));assert(frame.rx_capture_time_us==1000000);
 }else{
  stack.process_rx_path_(frame);stack.process_aec_and_callbacks_(frame);
  assert(stack.seen==1000000);
  frame.ratio=3;stack.process_rx_path_(frame);stack.process_aec_and_callbacks_(frame);
  assert(frame.rx_capture_time_us>0&&stack.seen==0);
  frame.ratio=1;frame.processor_enabled=true;frame.processor_ready=true;
  stack.process_rx_path_(frame);assert(frame.output_capture_time_us>0);
  stack.process_aec_and_callbacks_(frame);assert(stack.seen==0&&stack.processed==1);
  frame.processor_ready=false;stack.process_rx_path_(frame);stack.process_aec_and_callbacks_(frame);
  assert(stack.seen==0&&stack.processed==1);
 }
}
'''
    cpp = directory / "clock.cpp"
    cpp.write_text(source)
    binary = directory / "clock"
    built = subprocess.run(
        ["g++", "-std=c++17", "-O1", "-g", "-fsanitize=address,undefined",
         "-I", str(directory), str(cpp), "-o", str(binary)],
        capture_output=True, text=True, timeout=60,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    return binary


@pytest.mark.parametrize("scenario", ["partial", "overflow", "wrap", "lifetime", "mapping"])
def test_dma_capture_time_or_truthful_unknown(capture_clock_binary, scenario):
    result = subprocess.run(
        [str(capture_clock_binary), scenario], capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
