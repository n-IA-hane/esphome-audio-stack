"""Constant microphone gain must preserve low-level PCM from the first sample."""

from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "esphome/components/esp_audio_stack"


def function(source: str, signature: str) -> str:
    start = source.index(signature)
    opening = source.index("{", start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


def test_linear_mic_gain_pcm_contract(tmp_path):
    main = (COMPONENT / "esp_audio_stack.cpp").read_text()
    pipeline = (COMPONENT / "audio_pipeline.cpp").read_text()
    helpers = "\n".join(
        function(main, signature)
        for signature in (
            "int32_t attenuation_factor_to_q31(",
            "float sanitize_gain_factor(",
        )
    )
    source = r'''
#include "audio_core_audio_utils.h"
#include <algorithm>
#include <atomic>
#include <cassert>
#include <climits>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <new>
#include <vector>
static size_t allocations=0;
void* operator new(size_t n){++allocations;if(auto p=std::malloc(n))return p;throw std::bad_alloc();}
void operator delete(void* p)noexcept{std::free(p);}
void operator delete(void* p,size_t)noexcept{std::free(p);}
namespace esp_audio_libs::gain {
unsigned attenuation_calls=0;
int32_t db_to_q31(float db){return static_cast<int32_t>(std::llround(std::pow(10.0,double(db)/20.0)*INT32_MAX));}
void apply(const uint8_t* in,uint8_t* out,int32_t q31,size_t samples,size_t bytes){
 assert(bytes==2);++attenuation_calls;
 for(size_t i=0;i<samples;++i){int16_t x;std::memcpy(&x,in+i*2,2);
  int16_t y=static_cast<int16_t>(std::nearbyint(double(x)*q31/INT32_MAX));std::memcpy(out+i*2,&y,2);}
}
}
namespace esphome::esp_audio_stack {
struct ESPAudioStack {
 struct AudioTaskCtx {
  int16_t* output_buffer=nullptr;size_t current_output_frame_size=0,current_output_frame_bytes=0;
  int32_t mic_gain_q31=INT32_MAX;uint32_t mic_gain_boost_q16=1U<<16;
 };
 std::atomic<float> mic_gain_{1};std::atomic<int32_t> mic_gain_q31_{INT32_MAX};
 std::atomic<uint32_t> mic_gain_boost_q16_{1U<<16};
 void set_mic_gain(float);void apply_post_processor_gain_(AudioTaskCtx&);
};
'''
    source += helpers
    source += function(main, "void ESPAudioStack::set_mic_gain(")
    source += function(pipeline, "void ESPAudioStack::apply_post_processor_gain_(")
    source += r'''
}
using namespace esphome::esp_audio_stack;
int16_t reference(int16_t x,uint32_t q16){
 double y=std::nearbyint(double(x)*q16/65536.0);
 return static_cast<int16_t>(std::clamp(y,-32768.0,32767.0));
}
int main(){
 ESPAudioStack stack;
 const float factors[]={1.0f,1.0001f,1.0592537f,1.5f,2.818383f,31.622776f,32.0f};
 for(float factor:factors){
  stack.set_mic_gain(factor);assert(stack.mic_gain_.load()==factor);
  assert(stack.mic_gain_q31_.load()==INT32_MAX);
  const uint32_t q16=stack.mic_gain_boost_q16_.load();
  for(int sample=-32768;sample<=32767;++sample){
   int16_t output=sample;const size_t before=allocations;
   apply_microphone_boost_q16(&output,1,q16);
   assert(output==reference(sample,q16));assert(allocations==before);
  }
 }
 // Tiny first-syllable samples must be scaled immediately, with no warmup.
 int16_t quiet[]={20,-41,15,-32,0};
 stack.set_mic_gain(std::pow(10.0f,9.0f/20.0f));
 ESPAudioStack::AudioTaskCtx ctx;ctx.output_buffer=quiet;ctx.current_output_frame_size=5;ctx.current_output_frame_bytes=10;
 ctx.mic_gain_q31=stack.mic_gain_q31_.load();ctx.mic_gain_boost_q16=stack.mic_gain_boost_q16_.load();
 const size_t before=allocations;stack.apply_post_processor_gain_(ctx);assert(allocations==before);
 assert(quiet[0]==56&&quiet[1]==-116&&quiet[2]==42&&quiet[3]==-90&&quiet[4]==0);
 assert(esp_audio_libs::gain::attenuation_calls==0);
 // Round signed half-way samples to even, with exact zero and signed symmetry.
 int16_t half[]={1,-1,3,-3,5,-5,0};apply_microphone_boost_q16(half,7,microphone_boost_to_q16(1.5f));
 const int16_t even[]={2,-2,4,-4,8,-8,0};assert(std::equal(half,half+7,even));
 // All chunk boundaries, channel layouts and preceding signal histories are irrelevant.
 std::vector<int16_t> input(8192),whole,chunked,left(4096),right(4096);
 for(size_t i=0;i<input.size();++i)input[i]=static_cast<int16_t>((i*137U+17U)&65535U);
 whole=input;chunked=input;const uint32_t factor=microphone_boost_to_q16(2.818383f);
 apply_microphone_boost_q16(whole.data(),whole.size(),factor);
 const size_t chunks[]={1,7,160,256,1024,3};size_t position=0,index=0;
 while(position<chunked.size()){size_t n=std::min(chunks[index++%6],chunked.size()-position);
  apply_microphone_boost_q16(chunked.data()+position,n,factor);position+=n;}
 assert(whole==chunked);
 for(size_t i=0;i<left.size();++i){left[i]=input[i*2];right[i]=input[i*2+1];}
 apply_microphone_boost_q16(left.data(),left.size(),factor);apply_microphone_boost_q16(right.data(),right.size(),factor);
 for(size_t i=0;i<left.size();++i){assert(left[i]==whole[i*2]);assert(right[i]==whole[i*2+1]);}
 int16_t silence[256]={};for(unsigned repeat=0;repeat<200;++repeat)apply_microphone_boost_q16(silence,256,2097152);
 for(int16_t sample:silence)assert(sample==0);
 // The public setter keeps attenuation, sanitization and existing limits.
 for(float invalid:{-1.f,0.f,std::numeric_limits<float>::quiet_NaN(),std::numeric_limits<float>::infinity()}){
  stack.set_mic_gain(invalid);assert(stack.mic_gain_.load()==0&&stack.mic_gain_q31_.load()==0);
  int16_t mute[]={123,-321};ctx.output_buffer=mute;ctx.current_output_frame_size=2;ctx.current_output_frame_bytes=4;
  ctx.mic_gain_q31=stack.mic_gain_q31_.load();ctx.mic_gain_boost_q16=stack.mic_gain_boost_q16_.load();
  stack.apply_post_processor_gain_(ctx);assert(mute[0]==0&&mute[1]==0);
 }
 stack.set_mic_gain(.5f);int16_t attenuate[]={1000,-1000};ctx.output_buffer=attenuate;
 ctx.mic_gain_q31=stack.mic_gain_q31_.load();ctx.mic_gain_boost_q16=stack.mic_gain_boost_q16_.load();
 stack.apply_post_processor_gain_(ctx);assert(attenuate[0]==500&&attenuate[1]==-500);
 assert(esp_audio_libs::gain::attenuation_calls==1);
 stack.set_mic_gain(1000.f);assert(stack.mic_gain_.load()==32.f&&stack.mic_gain_boost_q16_.load()==2097152);
}
'''
    cpp = tmp_path / "mic_gain.cpp"
    cpp.write_text(source)
    command = [
        "g++", "-std=c++17", "-O1", "-Wall", "-Wextra", "-Werror",
        "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
        "-I", str(COMPONENT), str(cpp), "-o", str(tmp_path / "mic_gain"),
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)
    subprocess.run([str(tmp_path / "mic_gain")], check=True, capture_output=True, text=True)
