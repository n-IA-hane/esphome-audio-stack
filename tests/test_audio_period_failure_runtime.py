"""A failed RX conversion must retire its period before any consumer or TX."""
from pathlib import Path
import subprocess

import pytest

from test_volume_reference_runtime import method

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "esphome/components/esp_audio_stack"


@pytest.mark.parametrize("channels,bits", [(1, 16), (1, 32), (2, 16), (2, 32)])
@pytest.mark.parametrize("fault", ["conversion", "read", "healthy"])
def test_real_rx_period_preserves_pcm_validity_and_clock(tmp_path, channels, bits, fault):
    source = (COMPONENT / "audio_pipeline.cpp").read_text()
    methods = "\n".join(method(source, signature) for signature in (
        "void ESPAudioStack::process_rx_path_(",
        "bool ESPAudioStack::process_rx_stereo_slot_(",
        "bool ESPAudioStack::process_rx_mono_effects_(",
        "void ESPAudioStack::fail_audio_session_(",
    ))
    start = source.index("    if (ctx.need_rx_processing) {", source.index("// Snapshot atomic state"))
    end = source.index("\n#ifdef USE_AUDIO_PROCESSOR", start)
    period = source[start:end]
    harness = r'''
#include <algorithm>
#include <atomic>
#include <cassert>
#include <cstdint>
#include <vector>
#include "audio_slot_layout.h"
using esphome::esp_audio_stack::AudioRxLayout;
#define USE_ESP_AUDIO_STACK_MONO_RX
#define USE_ESP_AUDIO_STACK_MULTI_RX
#define USE_ESP_AUDIO_STACK_STD_DUAL_MIC
#define ESP_LOGE(...) ((void)0)
struct Converter {
 bool ok=true;
 template<class T> bool convert(const T *in,size_t n,size_t stride,const uint8_t *slots,
                               int16_t *interleaved,int16_t *mono,int16_t*,uint8_t channels) {
  if(!ok)return false;
  for(size_t i=0;i<n;i++)for(unsigned c=0;c<channels;c++){
   int16_t value=sizeof(T)==4 ? int16_t(in[i*stride+slots[c]]>>16):int16_t(in[i*stride+slots[c]]);
   if(interleaved)interleaved[i*channels+c]=value;
   if(c==0)mono[i]=value;
  }
  return true;
 }
 bool process_multi(const int16_t *p,size_t n,uint8_t s,const uint8_t *o,int16_t*a,int16_t*m,int16_t*r,uint8_t c){return convert(p,n,s,o,a,m,r,c);}
 bool process_multi_32(const int32_t*p,size_t n,uint8_t s,const uint8_t *o,int16_t*a,int16_t*m,int16_t*r,uint8_t c){return convert(p,n,s,o,a,m,r,c);}
 bool process_planar(const int16_t*,size_t,uint8_t,const uint8_t*,int16_t*,int16_t*,int16_t*,uint8_t){assert(false);return false;}
 bool process_planar_32(const int32_t*,size_t,uint8_t,const uint8_t*,int16_t*,int16_t*,int16_t*,uint8_t){assert(false);return false;}
 bool process_strided_32(const int32_t*p,int16_t*m,size_t n,uint8_t,uint8_t){uint8_t o=0;return convert(p,n,1,&o,nullptr,m,nullptr,1);}
 bool process(const int16_t*p,int16_t*m,size_t n){uint8_t o=0;return convert(p,n,1,&o,nullptr,m,nullptr,1);}
};
struct AudioTaskCtx {
 int16_t *rx_buffer=nullptr,*mic_buffer=nullptr,*processor_mic_buffer=nullptr,*output_buffer=nullptr;
 const int16_t *processor_input=nullptr;
 size_t input_frame_size=4,bus_frame_size=4;uint8_t i2s_bps=2,processor_mic_channels=1,rx_rate_converter_channels=1;
 int std_second_mic_slot=-1,ratio=2;
 AudioRxLayout rx_layout;
 bool need_rx_processing=true,need_rx_drain=false,clock_only_tx=true,need_tx_audio=false;
 bool processor_planar_input=false;
};
struct ESPAudioStack {
 std::atomic<bool> has_i2s_error_{false},audio_stack_running_{true};
 Converter rx_rate_converter_,mic_rate_converter_;
 bool read_ok=true;unsigned conditioning=0,dispatches=0,clocks=0;
 void process_rx_path_(AudioTaskCtx&);void fail_audio_session_(const char*);
 bool process_rx_stereo_slot_(AudioTaskCtx&);bool process_rx_mono_effects_(AudioTaskCtx&);
 bool read_rx_frame_(AudioTaskCtx&,const char*){return read_ok;}
 bool convert_rx_frame_(AudioTaskCtx& c){return c.processor_mic_channels==2?process_rx_stereo_slot_(c):process_rx_mono_effects_(c);}
 void apply_input_conditioning_(AudioTaskCtx&){++conditioning;}
 void drain_rx_minimal_(AudioTaskCtx&){assert(false);}
 void process_aec_and_callbacks_(AudioTaskCtx&c){
  if(c.output_buffer==nullptr)return;
  ++dispatches;
  // A dual consumer really reads all interleaved samples, under ASan/UBSan.
  for(size_t i=0;i<c.input_frame_size*c.processor_mic_channels;i++)assert(c.processor_input[i]==int16_t(100+i));
 }
 void process_tx_clock_only_(AudioTaskCtx&){++clocks;}
 void process_tx_path_(AudioTaskCtx&){assert(false);}
 void period(AudioTaskCtx&ctx){for(unsigned once=0;once<1;once++){
// PRODUCTION_PERIOD
 }}
};
'''
    checks = r'''
int main(int argc,char **argv){
 const int channels=argv[1][0]-'0',bits=argv[2][0]=='3'?32:16;
 const char fault=argv[3][0];
 ESPAudioStack s;AudioTaskCtx c;
 std::vector<int16_t> mono(4,2222),dual(8,3333),raw16(8);
 std::vector<int32_t> raw32(8);
 for(unsigned i=0;i<8;i++){raw16[i]=100+i;raw32[i]=(100+i)<<16;}
 c.rx_buffer=bits==32?reinterpret_cast<int16_t*>(raw32.data()):raw16.data();
 c.mic_buffer=mono.data();c.processor_mic_buffer=dual.data();c.i2s_bps=bits/8;
 c.processor_mic_channels=channels;c.rx_rate_converter_channels=channels;c.std_second_mic_slot=channels==2?1:-1;
 c.rx_layout=AudioRxLayout::standard(channels==2,false,false,false,0,channels==2?1:-1);
 // Dispatch a complete real frame first, then retain its PCM as the stale witness.
 s.period(c);assert(s.dispatches==1&&s.clocks==1);
 unsigned previous=s.conditioning;
 if(fault=='c'){s.rx_rate_converter_.ok=false;s.mic_rate_converter_.ok=false;}
 if(fault=='r')s.read_ok=false;
 s.period(c);
 if(fault=='c'){
  assert(!s.audio_stack_running_&&s.has_i2s_error_);
  assert(c.output_buffer==nullptr&&c.processor_input==nullptr&&"failed conversion must invalidate both PCM views");
  assert(s.dispatches==1&&s.clocks==1&&s.conditioning==previous);
 }else if(fault=='r'){
  assert(s.audio_stack_running_&&!s.has_i2s_error_);
  assert(c.output_buffer==nullptr&&c.processor_input==nullptr);
  assert(s.dispatches==1&&s.clocks==2&&s.conditioning==previous);
 }else assert(s.dispatches==2&&s.clocks==2&&s.conditioning==previous+1);
}
'''
    cpp = tmp_path / "period.cpp"
    cpp.write_text(harness.replace("// PRODUCTION_PERIOD", period) + methods + checks)
    binary = tmp_path / "period"
    compiled = subprocess.run(["g++", "-std=c++17", "-O1", "-g", "-fsanitize=address,undefined",
                               "-I" + str(COMPONENT), str(cpp), "-o", str(binary)], capture_output=True, text=True)
    assert compiled.returncode == 0, compiled.stderr
    run = subprocess.run([str(binary), str(channels), str(bits), fault], capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
