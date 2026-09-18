"""Execute production TX formatting and failed-open cleanup with driver witnesses."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1] / 'esphome/components/esp_audio_stack'


def test_tdm_transmits_samples_without_unused_slot_padding(tmp_path):
    source = (ROOT / 'audio_pipeline.cpp').read_text()
    start = source.index('bool ESPAudioStack::format_tx_frame_(')
    end = source.index('\n// ', start)
    body = source[start:end]
    code = r'''
#include <cstddef>
#include <cstdint>
#include <cassert>
#define ESP_LOGE(...) ((void)0)
#define USE_ESP_AUDIO_STACK_32BIT
struct AudioTaskCtx {
 bool use_tdm_bus; uint8_t num_ch, i2s_bps; size_t bus_frame_size;
 int16_t *spk_buffer; int32_t *tx_32_buffer;
};
struct ESPAudioStack {
 bool format_tx_frame_(AudioTaskCtx &, void **, size_t *);
 bool tx_bit_cvt_16_to_32_(uint8_t ch,const void *in,uint32_t n,void *out) {
  assert(ch==1); for(size_t i=0;i<n;i++) static_cast<int32_t*>(out)[i]=static_cast<const int16_t*>(in)[i]*65536;return true;
 }
};
''' + body + r'''
int main() {
 ESPAudioStack s;int16_t pcm[]={123,-456,789};int32_t wide[3]{};
 AudioTaskCtx c{true,1,2,3,pcm,wide};void *out=nullptr;size_t bytes=0;
 assert(s.format_tx_frame_(c,&out,&bytes));assert(out==pcm);assert(bytes==6);
 c.i2s_bps=4;assert(s.format_tx_frame_(c,&out,&bytes));assert(bytes==12);
 assert(wide[0]==123*65536&&wide[1]==-456*65536&&wide[2]==789*65536);
}
'''
    cpp = tmp_path / 'witness.cpp'
    cpp.write_text(code)
    exe=tmp_path/'witness'
    subprocess.run(['c++','-std=c++17',str(cpp),'-o',str(exe)],check=True)
    subprocess.run([str(exe)],check=True)


def test_failed_open_releases_enabled_channels_and_preserves_failed_handles(tmp_path):
    source=(ROOT/'esp_audio_stack.cpp').read_text()
    start=source.index('  auto release_channel =',source.index('void ESPAudioStack::deinit_i2s_'))
    end=source.index('  const bool tx_released',start)
    code=r'''
#include <cassert>
using esp_err_t=int;
#define ESP_OK 0
#define ESP_LOGE(...) ((void)0)
struct Channel {bool enabled;bool fail_delete;int disabled=0;int deleted=0;};
using i2s_chan_handle_t=Channel*;
struct i2s_chan_info_t {bool is_enabled;};
int i2s_channel_get_info(Channel *c,i2s_chan_info_t *i){i->is_enabled=c->enabled;return 0;}
int i2s_channel_disable(Channel *c){assert(c->enabled);c->enabled=false;c->disabled++;return 0;}
int i2s_del_channel(Channel *c){assert(!c->enabled);if(c->fail_delete)return 1;c->deleted++;return 0;}
int main(){
''' + source[start:end] + r'''
 Channel running{true,false};auto a=&running;assert(release_channel(a));assert(a==nullptr);assert(running.disabled==1&&running.deleted==1);
 Channel ready{false,false};auto b=&ready;assert(release_channel(b));assert(b==nullptr);assert(ready.disabled==0&&ready.deleted==1);
 Channel failure{true,true};auto c=&failure;assert(!release_channel(c));assert(c==&failure);assert(failure.disabled==1);
 failure.fail_delete=false;assert(release_channel(c));assert(c==nullptr);assert(failure.disabled==1&&failure.deleted==1);
}
'''
    cpp = tmp_path / 'cleanup.cpp'
    cpp.write_text(code)
    exe = tmp_path / 'cleanup'
    subprocess.run(['c++','-std=c++17',str(cpp),'-o',str(exe)],check=True)
    subprocess.run([str(exe)],check=True)
