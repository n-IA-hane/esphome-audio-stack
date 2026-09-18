"""Validate AFE channel roles independently of transport and physical I2S slots."""
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]

def test_afe_input_layouts_preserve_microphones_reference_and_padding(tmp_path):
    src=(ROOT/'esphome/components/esp_afe/esp_afe.cpp').read_text()
    start=src.index('static inline void stage_afe_input_frame(')
    end=src.index('\nstatic int effective_feed_task_stack_size',start)
    program=r'''
#include <cstdint>
#include <cassert>
#include <initializer_list>
struct afe_pcm_config_t {int total_ch_num;int mic_num;uint8_t *mic_ids;int ref_num;uint8_t *ref_ids;};
''' + src[start:end] + r'''
void check(afe_pcm_config_t cfg,int stride,const int16_t *mic,const int16_t *ref,std::initializer_list<int16_t> expected) {
 int16_t data[10];for(auto &x:data)x=12345;
 stage_afe_input_frame(data+1,mic,ref,2,stride,cfg);
 size_t i=1;for(auto x:expected)assert(data[i++]==x);
 assert(data[0]==12345&&data[i]==12345);
}
int main(){
 int16_t mono[]={10,20},dual[]={10,11,20,21},ref[]={90,91};
 uint8_t mic0[]={0},mic01[]={0,1},mic12[]={1,2};uint8_t r1[]={1},r2[]={2},r3[]={3},r0[]={0};
 check({2,1,mic0,1,r1},1,mono,ref,{10,90,20,91});
 check({3,1,mic0,1,r2},1,mono,ref,{10,0,90,20,0,91});
 check({3,2,mic01,1,r2},2,dual,ref,{10,11,90,20,21,91});
 check({4,2,mic01,1,r3},2,dual,ref,{10,11,0,90,20,21,0,91});
 check({3,2,mic12,1,r0},2,dual,ref,{90,10,11,91,20,21});
 check({4,2,mic01,1,r3},1,mono,ref,{10,0,0,90,20,0,0,91});
 check({3,2,mic01,1,r2},2,dual,nullptr,{10,11,0,20,21,0});
}
'''
    cpp = tmp_path / 'layout.cpp'
    cpp.write_text(program)
    exe = tmp_path / 'layout'
    subprocess.run(['c++','-std=c++17','-include','cstddef',str(cpp),'-o',str(exe)],check=True)
    subprocess.run([str(exe)],check=True)
