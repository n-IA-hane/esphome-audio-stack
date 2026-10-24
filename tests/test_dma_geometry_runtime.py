"""Explicit supported DMA divisors must retain their selected cadence."""
from pathlib import Path
import subprocess
import sys

import pytest
import yaml
from test_facade_pcm_config import base_config
from test_volume_reference_runtime import method

COMPONENT=Path(__file__).resolve().parents[1]/'esphome/components/esp_audio_stack'


@pytest.mark.parametrize('requested',[64,128,256])
def test_explicit_valid_dma_divisor_is_preserved(tmp_path,requested):
    source=(COMPONENT/'esp_audio_stack.cpp').read_text()
    a=source.index('  uint32_t dma_frame_num =')
    b=source.index('#if SOC_I2S_SUPPORTS_TDM',a)
    code=r'''
#include <algorithm>
#include <cassert>
#include <cstdint>
#define ESP_LOGE(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
#define ESP_LOGD(...) ((void)0)
constexpr unsigned DMA_BUFFER_DURATION_MS=10;
enum class I2SHardwareState {ERROR};
''' + method(source,'uint32_t largest_divisor_at_most(') + r'''
struct ESPAudioStack {
 bool dma_frame_num_configured_=true,use_tdm_bus_=false;unsigned dma_frame_num_=128,sample_rate_=16000,rate_conversion_ratio_=1,tdm_total_slots_=4;
 void set_i2s_hardware_state_(I2SHardwareState){}
 bool select(unsigned &selected){unsigned max_bytes_per_frame=2,bytes_per_sample=2;
''' + source[a:b] + r'''
 selected=dma_frame_num;return true;}
};
'''+f'int main(){{ESPAudioStack s;s.dma_frame_num_={requested};unsigned selected=0;assert(s.select(selected));assert(selected=={requested}&&"a valid explicit divisor must not be silently enlarged");}}'
    cpp=tmp_path/'geometry.cpp'
    cpp.write_text(code)
    exe=tmp_path/'geometry'
    result=subprocess.run(['g++','-std=c++17',str(cpp),'-o',str(exe)],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    result=subprocess.run([str(exe)],capture_output=True,text=True)
    assert result.returncode==0,result.stderr


@pytest.mark.parametrize('explicit', [False, True])
@pytest.mark.parametrize('layout', ['speaker-only', 'clock-only', 'duplex', 'dual-bus'])
def test_tx_and_rx_allocate_independent_dma_depths(tmp_path, explicit, layout):
    source = (COMPONENT / 'esp_audio_stack.cpp').read_text()
    start = source.index('  const uint32_t tx_dma_desc_num =')
    end = source.index('\n#if SOC_I2S_SUPPORTS_TDM', start)
    code = r'''
#include <cassert>
#include <cstdint>
#include <vector>
#define ESP_LOGE(...) ((void)0)
using i2s_port_t=unsigned;
using esp_err_t=int;
constexpr int ESP_OK=0;
enum class I2SHardwareState {ERROR};
struct Config {unsigned id=0,dma_desc_num=6,dma_frame_num=128;};
struct Call {Config config;bool tx;};std::vector<Call> calls;
int i2s_new_channel(Config *config,void **tx,void **rx){
 calls.push_back({*config,tx!=nullptr});if(tx)*tx=reinterpret_cast<void*>(1);if(rx)*rx=reinterpret_cast<void*>(2);return ESP_OK;
}
struct ESPAudioStack{
 bool dma_desc_num_configured_=false;unsigned dma_desc_num_=6;void *tx_handle_=nullptr,*rx_handle_=nullptr;
 void set_i2s_hardware_state_(I2SHardwareState){}void deinit_i2s_(){}
 bool allocate(bool need_tx,bool need_rx,unsigned tx_i2s_num,unsigned rx_i2s_num){
  Config chan_cfg;unsigned dma_desc_num=10;
''' + source[start:end] + r'''
 return true;}
};
'''
    receive = layout != 'speaker-only'
    rx_port = 1 if layout == 'dual-bus' else 0
    code += f'''int main(){{ESPAudioStack owner;owner.dma_desc_num_configured_={str(explicit).lower()};
 assert(owner.allocate(true,{str(receive).lower()},0,{rx_port}));
 assert(calls.size()=={2 if receive else 1});assert(calls[0].tx);
 assert(calls[0].config.dma_desc_num=={6 if explicit else 2});
'''
    if receive:
        code += f'assert(!calls[1].tx && calls[1].config.id=={rx_port} && calls[1].config.dma_desc_num==10);'
    code += '}'
    cpp = tmp_path / 'allocation.cpp'
    cpp.write_text(code)
    binary = tmp_path / 'allocation'
    result = subprocess.run(['g++', '-std=c++17', str(cpp), '-o', str(binary)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    result = subprocess.run([str(binary)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('requested', [None, 6])
def test_dma_descriptor_codegen_preserves_omission_and_explicit_value(tmp_path, requested):
    document = base_config(tmp_path)
    if requested is not None:
        document['esp_audio_stack']['dma_desc_num'] = requested
    config = tmp_path / 'device.yaml'
    config.write_text(yaml.safe_dump(document, sort_keys=False))
    result = subprocess.run(
        [sys.executable, '-m', 'esphome', 'compile', '--only-generate', str(config)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    generated = (tmp_path / 'build/src/main.cpp').read_text()
    assert ('set_dma_desc_num(6)' in generated) == (requested is not None)
