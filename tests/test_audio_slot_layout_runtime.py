"""Sparse DMA must preserve physical slot identity, including diagnostics."""
from pathlib import Path
import subprocess
ROOT=Path(__file__).resolve().parents[1]/'esphome/components/esp_audio_stack'

def test_slot_layout_and_requested_diagnostic_channels(tmp_path):
    src=(ROOT/'esp_audio_stack.cpp').read_text()
    start=src.index('AudioSlotLayout ESPAudioStack::make_tdm_rx_layout_()')
    end=src.index('\n}\n#endif',start)+2
    program=r'''
#include <cassert>
#include <vector>
#include "audio_slot_layout.h"
using namespace esphome::esp_audio_stack;
#define USE_ESP_AUDIO_STACK_SLOT_LEVELS
struct ESPAudioStack {
 uint8_t tdm_mic_slot_=0,tdm_ref_slot_=1,tdm_total_slots_=4;
 int8_t tdm_second_mic_slot_=2;bool use_tdm_ref_=true;
 bool tdm_slot_level_sensor_enabled_[8]{};
 AudioSlotLayout make_tdm_rx_layout_() const;
};
''' + src[start:end] + r'''
int main(){
 for(unsigned mask=1;mask<256;mask++) {
  std::vector<unsigned> physical;
  for(unsigned bit=0;bit<8;bit++)if(mask&(1U<<bit))physical.push_back(bit);
  AudioSlotLayout layout{uint16_t(mask)};assert(layout.count()==physical.size());
  for(unsigned pos=0;pos<physical.size();pos++)assert(layout.index(physical[pos])==pos);
  for(unsigned bit=0;bit<8;bit++)if(!(mask&(1U<<bit)))assert(layout.index(bit)==AudioSlotLayout::INVALID);
 }
 ESPAudioStack s;assert(s.make_tdm_rx_layout_().mask==7);
 s.tdm_slot_level_sensor_enabled_[3]=true;assert(s.make_tdm_rx_layout_().mask==15);
 s.tdm_second_mic_slot_=-1;s.use_tdm_ref_=false;assert(s.make_tdm_rx_layout_().mask==9);
 assert(s.make_tdm_rx_layout_().index(3)==1);
}
'''
    cpp=tmp_path/'slots.cpp';cpp.write_text(program);exe=tmp_path/'slots'
    subprocess.run(['c++','-std=c++17','-I',str(ROOT),str(cpp),'-o',str(exe)],check=True)
    subprocess.run([str(exe)],check=True)
