"""Measure STD/TDM channel selection using the actual shared C++ observer."""

from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_slot_levels_keep_physical_channel_identity(tmp_path):
    source = (ROOT / "esphome/components/esp_audio_stack/audio_pipeline.cpp").read_text()
    start = source.index("void ESPAudioStack::update_tdm_slot_levels_(")
    method = source[start:source.index("\n}\n", start) + 2]
    harness = r'''
#include <algorithm>
#include <atomic>
#include <cassert>
#include <cmath>
#include <cstdint>
float compute_rms_dbfs_i16(const int16_t *p,size_t n,size_t stride) {
 double power=0;for(size_t i=0;i<n;++i)power+=double(p[i*stride])*p[i*stride];
 return power==0 ? -120 : 10*std::log10(power/n/(32768.0*32768.0));
}
float compute_rms_dbfs_i32_top16(const int32_t *p,size_t n,size_t stride) {
 double power=0;for(size_t i=0;i<n;++i){double x=p[i*stride]/65536;power+=x*x;}
 return power==0 ? -120 : 10*std::log10(power/n/(32768.0*32768.0));
}
struct AudioTaskCtx {bool use_tdm_bus=false;uint8_t tdm_total_slots=4;uint8_t tdm_rx_active_slots=0;size_t bus_frame_size=8;unsigned i2s_bps=2;int16_t *rx_buffer;};
struct ESPAudioStack {
 bool tdm_slot_level_sensor_enabled_[8]{};
 std::atomic<float> tdm_slot_level_dbfs_[8]{};
 uint8_t tdm_slot_level_divider_=0;
 uint16_t tdm_rx_slot_mask_() const {return 0x0D;} // physical slots 0, 2, 3
 static uint8_t tdm_packed_slot_index_(uint16_t mask,uint8_t physical_slot) {
  uint16_t lower=physical_slot==0 ? 0 : uint16_t(mask&((1U<<physical_slot)-1U));
  return __builtin_popcount(unsigned(lower));
 }
 void update_tdm_slot_levels_(const AudioTaskCtx&);
};
'''
    checks = r'''
int main(){
 for(unsigned width : {2,4}) {
  ESPAudioStack stack;AudioTaskCtx ctx;ctx.i2s_bps=width;
  int16_t raw[32]{};int32_t wide[32]{};unsigned stride=2;
  stack.tdm_slot_level_sensor_enabled_[0]=true;stack.tdm_slot_level_sensor_enabled_[1]=true;
  for(unsigned i=0;i<8;++i){raw[i*stride]=8192;raw[i*stride+1]=16384;}
  for(unsigned i=0;i<32;++i)wide[i]=int32_t(raw[i])*65536;
  ctx.rx_buffer=width==2 ? raw : reinterpret_cast<int16_t*>(wide);
  for(unsigned i=0;i<8;++i)stack.update_tdm_slot_levels_(ctx);
  assert(std::abs(stack.tdm_slot_level_dbfs_[0]+12.0412f)<0.01f);
  assert(std::abs(stack.tdm_slot_level_dbfs_[1]+6.0206f)<0.01f);
  assert(stack.tdm_slot_level_dbfs_[2]==0); // Unrequested slots are not measured.
 }
 for(unsigned width : {2,4}) {
  ESPAudioStack stack;AudioTaskCtx ctx;ctx.use_tdm_bus=true;ctx.i2s_bps=width;ctx.tdm_rx_active_slots=3;
  int16_t raw[32]{};int32_t wide[32]{};
  for(unsigned slot=0;slot<4;++slot)stack.tdm_slot_level_sensor_enabled_[slot]=true;
  for(unsigned i=0;i<8;++i){raw[i*3]=8192;raw[i*3+1]=16384;raw[i*3+2]=24576;}
  for(unsigned i=0;i<32;++i)wide[i]=int32_t(raw[i])*65536;
  ctx.rx_buffer=width==2 ? raw : reinterpret_cast<int16_t*>(wide);
  for(unsigned i=0;i<8;++i)stack.update_tdm_slot_levels_(ctx);
  assert(std::abs(stack.tdm_slot_level_dbfs_[0]+12.0412f)<0.01f);
  assert(stack.tdm_slot_level_dbfs_[1]==-120); // Disabled physical slot has no DMA sample.
  assert(std::abs(stack.tdm_slot_level_dbfs_[2]+6.0206f)<0.01f);
  assert(std::abs(stack.tdm_slot_level_dbfs_[3]+2.4988f)<0.01f);
 }
}
'''
    cpp = tmp_path / "levels.cpp"
    cpp.write_text(harness + method + checks)
    exe = tmp_path / "levels"
    subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                    str(cpp), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
