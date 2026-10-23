"""STD slot validation and production C++ dispatch, without physical MEMS."""

import importlib.util
from pathlib import Path
import subprocess

import pytest
from esphome import config_validation as cv
from esphome.core import ID
import esphome.final_validate as fv

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "esphome/components/esp_audio_stack"
spec = importlib.util.spec_from_file_location("std_audio_schema", COMPONENT / "__init__.py")
schema = importlib.util.module_from_spec(spec)
spec.loader.exec_module(schema)


@pytest.fixture(autouse=True)
def final_validation_context():
    token = fv.full_config.set({})
    yield
    fv.full_config.reset(token)


@pytest.mark.parametrize("slots", [["left", "right"], ["right", "left"]])
def test_std_slots_accept_both_orders(slots):
    config = {"rx_slot_mode": "stereo", "rx_mic_slots": slots}
    assert schema._validate_rx_mic_slots(config) is config


@pytest.mark.parametrize("extra", [
    {"rx_slot_mode": "mono"},
    {"rx_mic_slots": ["left", "left"]},
    {"use_stereo_aec_reference": True},
    {"tdm_mic_slots": [0, 2]},
])
def test_std_slots_reject_conflicting_layouts(extra):
    with pytest.raises(cv.Invalid):
        schema._validate_rx_mic_slots({
            "rx_slot_mode": "stereo", "rx_mic_slots": ["left", "right"], **extra,
        })


def test_dual_requirement_applies_to_the_bound_processor(monkeypatch):
    monkeypatch.setattr(schema, "get_esp32_variant", lambda: "ESP32S3")
    fv.full_config.set({"esp_afe": [
        {"id": ID("dual"), "mic_num": 2}, {"id": ID("mono"), "mic_num": 1},
    ]})
    for config in ({}, {"processor_id": ID("mono")}):
        assert schema._final_validate(config) is config
    with pytest.raises(cv.Invalid, match="requires"):
        schema._final_validate({"processor_id": ID("dual")})
    for layout in ({"rx_mic_slots": ["left", "right"]}, {"tdm_mic_slots": [0, 2]}):
        config = {"processor_id": ID("dual"), **layout}
        assert schema._final_validate(config) is config


@pytest.mark.parametrize("enabled", [False, True])
def test_actual_stereo_dispatch_selects_order_and_width(tmp_path, enabled):
    source = (COMPONENT / "audio_pipeline.cpp").read_text()
    start = source.index("bool ESPAudioStack::process_rx_stereo_slot_(")
    # Extract one definition, not the alternate #else implementation.
    method = source[start:source.index("\n}\n", start) + 2]
    harness = r'''
#include <cassert>
#include <cstdint>
#include <cstddef>
#include "audio_slot_layout.h"
using esphome::esp_audio_stack::AudioRxLayout;
struct Converter {
 bool ok=true;
 template<class T> bool convert(const T *in,size_t n,size_t stride,const uint8_t *slots,
                               int16_t *interleaved,int16_t *mono,int16_t *ref,uint8_t channels) {
   assert(ref==nullptr && stride==2);
   if(!ok) return false;
   for(size_t i=0;i<n;++i) for(unsigned ch=0;ch<channels;++ch) {
     int16_t value=sizeof(T)==4 ? int16_t(in[i*stride+slots[ch]] >> 16) : int16_t(in[i*stride+slots[ch]]);
     if(interleaved) interleaved[i*channels+ch]=value;
     if(ch==0) mono[i]=value;
   }
   return true;
 }
 bool process_multi(const int16_t *i,size_t n,size_t s,const uint8_t *o,int16_t *a,int16_t *m,int16_t *r,uint8_t c) {
   return convert(i,n,s,o,a,m,r,c);
 }
 bool process_multi_32(const int32_t *i,size_t n,size_t s,const uint8_t *o,int16_t *a,int16_t *m,int16_t *r,uint8_t c) {
   return convert(i,n,s,o,a,m,r,c);
 }
};
struct AudioTaskCtx {
 unsigned processor_mic_channels=1,rx_rate_converter_channels=1,i2s_bps=2;
 AudioRxLayout rx_layout=AudioRxLayout::standard(true,false,false,false,0,-1);
 size_t input_frame_size=3;
 int16_t *rx_buffer=nullptr,*mic_buffer=nullptr,*processor_mic_buffer=nullptr,*processor_input=nullptr;
#ifdef USE_ESP_AUDIO_STACK_STD_DUAL_MIC
 uint8_t std_primary_mic_slot=0; int8_t std_second_mic_slot=1;
#endif
};
struct ESPAudioStack {
 Converter rx_rate_converter_; bool mic_channel_right_=false; unsigned failures=0;
 void fail_audio_session_(const char*) {++failures;}
 bool process_rx_stereo_slot_(AudioTaskCtx&);
};
'''
    checks = r'''
int main() {
 ESPAudioStack stack; AudioTaskCtx ctx;
 int16_t raw[]={100,1000,200,2000,300,3000}, mono[3]{}, paired[6]{};
 ctx.rx_buffer=raw;ctx.mic_buffer=mono;ctx.processor_mic_buffer=paired;
 assert(stack.process_rx_stereo_slot_(ctx)); assert(mono[0]==100 && mono[2]==300);
 stack.mic_channel_right_=true;
 ctx.rx_layout=AudioRxLayout::standard(true,true,false,false,0,-1);
 assert(stack.process_rx_stereo_slot_(ctx)); assert(mono[0]==1000 && mono[2]==3000);
#ifdef USE_ESP_AUDIO_STACK_STD_DUAL_MIC
 ctx.processor_mic_channels=2;ctx.rx_rate_converter_channels=2;
 for(unsigned reverse=0;reverse<2;++reverse) {
  ctx.std_primary_mic_slot=reverse;ctx.std_second_mic_slot=1-reverse;
  ctx.rx_layout=AudioRxLayout::standard(true,false,false,false,reverse,1-reverse);
  assert(stack.process_rx_stereo_slot_(ctx));assert(ctx.processor_input==paired);
  for(unsigned i=0;i<3;++i) {assert(paired[2*i]==raw[2*i+reverse]);assert(paired[2*i+1]==raw[2*i+1-reverse]);assert(mono[i]==paired[2*i]);}
 }
 int32_t wide[6];for(unsigned i=0;i<6;++i)wide[i]=int32_t(raw[i])*65536;
 ctx.rx_buffer=reinterpret_cast<int16_t*>(wide);ctx.i2s_bps=4;
 assert(stack.process_rx_stereo_slot_(ctx));assert(paired[0]==1000 && paired[1]==100);
 ctx.rx_rate_converter_channels=1;assert(!stack.process_rx_stereo_slot_(ctx));assert(stack.failures==1);
 ctx.rx_rate_converter_channels=2;
#endif
 stack.rx_rate_converter_.ok=false;assert(!stack.process_rx_stereo_slot_(ctx));
}
'''
    cpp = tmp_path / "slots.cpp"
    cpp.write_text(harness + method + checks)
    exe = tmp_path / "slots"
    flags = ["-DUSE_ESP_AUDIO_STACK_STD_DUAL_MIC"] if enabled else []
    subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", *flags,
                    "-I" + str(COMPONENT), str(cpp), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
