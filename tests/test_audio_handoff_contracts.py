"""Exercise live production methods at RX/TX, consumer and reference boundaries.

Only I/O, scheduler and DSP algorithms are adapters. Method bodies are loaded
from the current checkout, never from the audit's frozen reproductions.
"""

from pathlib import Path
import subprocess

import pytest

from test_volume_reference_runtime import method
from test_audio_failure_runtime import failure_source

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "esphome/components/esp_audio_stack"


def run_cpp(tmp_path, source, *flags):
    cpp = tmp_path / "contract.cpp"
    cpp.write_text(source)
    binary = tmp_path / "contract"
    result = subprocess.run(
        ["g++", "-std=c++17", "-pthread", *flags, str(cpp), "-o", str(binary)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    result = subprocess.run([str(binary)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("failed_direction", ["rx", "tx"])
@pytest.mark.parametrize("invalid_state", [False, True])
def test_directional_io_failure_is_not_cleared_by_other_direction(
    tmp_path, failed_direction, invalid_state
):
    pipeline = (COMPONENT / "audio_pipeline.cpp").read_text()
    header = (COMPONENT / "esp_audio_stack.h").read_text()
    # Use the actual mutable error-counter declarations, including any future split.
    a = header.index("    // ── Loop mutable state")
    b = header.index("    // DC-HPF state", a)
    declarations = header[a:b]
    code = (
        r"""
#include <atomic>
#include <cassert>
#include <cstdint>
#include <cstring>
#define ESP_LOGE(...) ((void)0)
#define LOG_W_THROTTLED(...) ((void)0)
using esp_err_t=int;
constexpr int ESP_OK=0,ESP_FAIL=1,ESP_ERR_INVALID_STATE=2,portMAX_DELAY=0;
int rx_error=0,tx_error=0;
const char *esp_err_to_name(int){return "injected";}
int i2s_channel_read(void*,void *out,size_t n,size_t *got,int){*got=rx_error?0:n;if(!rx_error)memset(out,1,n);return rx_error;}
int i2s_channel_write(void*,void*,size_t n,size_t *sent,int){*sent=tx_error?0:n;return tx_error;}
struct AudioTaskCtx {
 int16_t storage[8]{},*rx_buffer=storage;size_t rx_frame_bytes=16;
"""
        + declarations
        + r"""
};
struct ESPAudioStack {
 void *rx_handle_=this,*tx_handle_=this;
 std::atomic<bool> audio_stack_running_{true},has_i2s_error_{false};
 bool read_rx_frame_(AudioTaskCtx&,const char*);
 bool write_tx_frame_(AudioTaskCtx&,void*,size_t);
};
"""
        + method(pipeline, "bool ESPAudioStack::read_rx_frame_(")
        + "\n"
        + method(pipeline, "bool ESPAudioStack::write_tx_frame_(")
    )
    code += f"\nint main(){{ AudioTaskCtx c; ESPAudioStack s; {failed_direction}_error={2 if invalid_state else 1};\n"
    code += r"""
 for(int i=0;i<101;i++) {s.read_rx_frame_(c,"test");s.write_tx_frame_(c,c.storage,sizeof(c.storage));}
 assert(s.has_i2s_error_ && !s.audio_stack_running_ && "healthy opposite direction must not mask persistent failure");
 // A successful operation of the SAME direction clears its own error streak.
 s.audio_stack_running_=true;s.has_i2s_error_=false;rx_error=0;tx_error=0;
 s.read_rx_frame_(c,"test");s.write_tx_frame_(c,c.storage,sizeof(c.storage));
 rx_error=1;for(int i=0;i<100;i++)s.read_rx_frame_(c,"test");
 assert(!s.has_i2s_error_);rx_error=0;s.read_rx_frame_(c,"test");
 rx_error=1;s.read_rx_frame_(c,"test");assert(!s.has_i2s_error_);
}
"""
    run_cpp(tmp_path, code)


def test_last_microphone_stop_unregisters_even_after_failed_session(tmp_path):
    code = failure_source().replace(
        "void unregister_mic_consumer(void*){}",
        "unsigned unregisters=0;void unregister_mic_consumer(void*){++unregisters;}",
    )
    code += r"""
int main(){
 ESPAudioStack p;p.start();p.service_audio_lifecycle_();p.finish_audio_lifecycle_();
 ESPAudioStackMicrophone m{&p};
 p.fail_audio_session_("RX");p.service_audio_lifecycle_();p.finish_audio_lifecycle_();
 m.loop();assert(m.parent_registered_ && m.active_listeners_==1);
 m.stop();m.loop();
 assert(m.active_listeners_==0 && !m.parent_registered_ && p.unregisters==1);
 for(int i=0;i<4;i++)m.loop();assert(p.unregisters==1);
 m.start();m.loop();p.service_audio_lifecycle_();p.finish_audio_lifecycle_();m.loop();
 assert(m.active_listeners_==1 && m.parent_registered_ && m.state_==microphone::STATE_RUNNING);
}
"""
    run_cpp(tmp_path, code)


def test_microphone_unmute_during_callback_is_not_overwritten(tmp_path):
    import esphome.components.microphone as upstream

    upstream_header = (Path(upstream.__file__).parent / "microphone.h").read_text()
    wrapper = method(upstream_header, "template<typename F> void add_data_callback(")
    cpp = (COMPONENT / "microphone/esp_audio_stack_microphone.cpp").read_text()
    code = (
        r"""
#include <cassert>
#include <cstring>
#include <cstdint>
#include <functional>
#include <vector>
#define LOG_W_THROTTLED(...) ((void)0)
namespace microphone {enum {STATE_STOPPED,STATE_RUNNING};}
struct Callbacks {
 std::vector<std::function<void(const std::vector<uint8_t>&)>> callbacks;
 template<class F>void add(F f){callbacks.emplace_back(f);}
 void call(const std::vector<uint8_t>& v){for(auto &f:callbacks)f(v);}
};
struct ESPAudioStackMicrophone {
 int state_=microphone::STATE_RUNNING;bool mute_state_=true;
 std::vector<uint8_t> audio_buffer_;Callbacks data_callbacks_;
 void set_mute_state(bool v){mute_state_=v;}
 void on_audio_data_(const uint8_t*,size_t);
"""
        + wrapper
        + "\n};\n"
        + method(cpp, "void ESPAudioStackMicrophone::on_audio_data_(")
        + r"""
int main(){
 ESPAudioStackMicrophone m;m.audio_buffer_.reserve(16);int calls=0;
 m.add_data_callback([&](const std::vector<uint8_t>&pcm){
  if(calls++==0){for(auto v:pcm)assert(v==0);m.set_mute_state(false);}
  else assert((pcm==std::vector<uint8_t>{1,2,3,4}));
 });
 const uint8_t pcm[]={1,2,3,4};m.on_audio_data_(pcm,4);
 assert(!m.mute_state_ && "dispatch must not overwrite the public unmute request");
 m.on_audio_data_(pcm,4);assert(calls==2);
 m.state_=microphone::STATE_STOPPED;m.on_audio_data_(pcm,4);assert(calls==2);
}
"""
    )
    run_cpp(tmp_path, code)


@pytest.mark.parametrize("ratio", [1, 3])
def test_previous_frame_follows_successful_tx_including_partial_and_clock_silence(
    tmp_path, ratio
):
    pipeline = (COMPONENT / "audio_pipeline.cpp").read_text()
    code = r"""
#include <atomic>
#include <algorithm>
#include <cstdint>
#include <cstring>
#include <cassert>
#include <vector>
#define USE_AUDIO_PROCESSOR
#define USE_ESP_AUDIO_STACK_MONO_REF
#define USE_ESP_AUDIO_STACK_PREVIOUS_FRAME_REF
#define ESP_LOGE(...) ((void)0)
namespace esp_audio_libs::gain {
 void apply(void *in,void *out,int32_t gain,size_t n,size_t){
  auto *a=static_cast<int16_t*>(in),*b=static_cast<int16_t*>(out);
  for(size_t i=0;i<n;i++)b[i]=static_cast<int16_t>((int64_t(a[i])*gain)>>31);
 }
}
struct AudioTaskCtx {
 size_t bus_frame_size=4,speaker_frame_bytes=8,speaker_got=0,input_frame_bytes=8,tdm_tx_frame_bytes=8;
 int16_t *spk_buffer=nullptr,*spk_ref_buffer=nullptr,*tx_clock_buffer=nullptr;
 int32_t hot_output_volume_q31=INT32_MAX;
 bool speaker_running=true,speaker_paused=false,speaker_underrun=false,processor_enabled=true;
 bool mic_running=true,use_tdm_bus=false;uint8_t num_ch=1,i2s_bps=2;
};
struct Ring {
 std::vector<int16_t> pending;
 size_t read(void *out,size_t cap,unsigned){size_t bytes=pending.size()*2;assert(bytes<=cap);
 if(bytes)memcpy(out,pending.data(),bytes);pending.clear();return bytes;}
};
struct Converter {
 unsigned calls=0,ratio=1;
 bool process(const int16_t *in,int16_t *out,size_t n){
 ++calls;for(size_t i=0;i<n/ratio;i++){int sum=0;for(unsigned k=0;k<ratio;k++)sum+=in[i*ratio+k];out[i]=sum/int(ratio);}return true;
 }
};
struct ESPAudioStack {
 void *tx_handle_=this;Ring ring;Ring *speaker_buffer_=&ring;
 int16_t saved[4]{};int16_t *direct_aec_ref_=saved;bool direct_aec_ref_valid_=false;
 std::atomic<bool> has_i2s_error_{false},audio_stack_running_{true};
 Converter play_ref_rate_converter_;std::vector<int16_t> wire;bool write_ok=true;
 void fail_audio_session_(const char*){has_i2s_error_=true;audio_stack_running_=false;}
 void process_tx_path_(AudioTaskCtx&);void process_tx_clock_only_(AudioTaskCtx&);
 void fill_mono_aec_reference_(AudioTaskCtx&);
 bool format_tx_frame_(AudioTaskCtx &ctx,void **data,size_t *n){*data=ctx.spk_buffer;*n=ctx.speaker_frame_bytes;return true;}
 bool write_tx_dma_blocks_(AudioTaskCtx&,void *data,size_t n,size_t){
  if(!write_ok)return false;auto *p=static_cast<int16_t*>(data);wire.assign(p,p+n/2);return true;
 }
};
"""
    # New helper is optional in the pre-fix source so the RED is behavioral.
    signature = "void ESPAudioStack::save_previous_tx_reference_("
    if signature in pipeline:
        code = code.replace(
            " void process_tx_path_",
            " void save_previous_tx_reference_(AudioTaskCtx&,const int16_t*);\n void process_tx_path_",
        )
        code += method(pipeline, signature) + "\n"
    for signature in (
        "void ESPAudioStack::process_tx_path_(",
        "void ESPAudioStack::process_tx_clock_only_(",
        "void ESPAudioStack::fill_mono_aec_reference_(",
    ):
        code += method(pipeline, signature) + "\n"
    code += f"\nint main(){{ ESPAudioStack s;AudioTaskCtx c;s.play_ref_rate_converter_.ratio={ratio};c.bus_frame_size=4*{ratio};c.speaker_frame_bytes=8*{ratio};\n"
    code += r"""
 int16_t speaker[12]{},clock[12]{},reference[4]{};c.spk_buffer=speaker;c.tx_clock_buffer=clock;c.spk_ref_buffer=reference;
 s.ring.pending.assign(c.bus_frame_size,1200);s.process_tx_path_(c);s.fill_mono_aec_reference_(c);
 for(auto v:reference)assert(v==1200);
 s.ring.pending.assign(c.bus_frame_size/2,600);s.process_tx_path_(c);s.fill_mono_aec_reference_(c);
 assert(reference[0]==600&&reference[1]==600&&reference[2]==0&&reference[3]==0);
 // Underrun and clock-only periods must advance the converter, not replay old speech.
 for(int i=0;i<4;i++){s.process_tx_path_(c);s.fill_mono_aec_reference_(c);for(auto v:reference)assert(v==0);}
 for(int i=0;i<3;i++){s.process_tx_clock_only_(c);s.fill_mono_aec_reference_(c);for(auto v:reference)assert(v==0);}
 assert(s.play_ref_rate_converter_.calls==9);
 // Post-volume reference is saved once, and only after successful physical write.
 c.hot_output_volume_q31=1<<30;s.ring.pending.assign(c.bus_frame_size,1000);
 s.process_tx_path_(c);s.fill_mono_aec_reference_(c);for(auto v:reference)assert(v==500);
 s.write_ok=false;s.ring.pending.assign(c.bus_frame_size,2000);s.process_tx_path_(c);
 assert(!s.direct_aec_ref_valid_);s.fill_mono_aec_reference_(c);for(auto v:reference)assert(v==0);
}
"""
    run_cpp(tmp_path, code)


@pytest.mark.parametrize("reference_right", [False, True])
@pytest.mark.parametrize("stereo_slot_mode", [False, True])
def test_raw_stereo_reference_selects_same_microphone_as_processing(
    tmp_path, reference_right, stereo_slot_mode
):
    pipeline = (COMPONENT / "audio_pipeline.cpp").read_text()
    capture = (COMPONENT / "audio_capture.cpp").read_text()
    has_layout = "ctx.rx_layout" in capture
    code = (
        r"""
#include <cassert>
#include <cstdint>
#include <vector>
#include <atomic>
#define USE_ESP_AUDIO_STACK_STEREO_REF
#define ESP_LOGE(...) ((void)0)
enum class CapturePoint {RAW_MIC};
using namespace esphome::esp_audio_stack;
struct AudioTaskCtx {
 bool rx_slot_mode_stereo=false,use_stereo_aec_ref=true,ref_channel_right=false;
 int16_t *rx_buffer=nullptr,*mic_buffer=nullptr,*spk_ref_buffer=nullptr;
 size_t bus_frame_size=3,input_frame_size=3;uint8_t i2s_bps=2;
"""
        + (" AudioRxLayout rx_layout;\n" if has_layout else "")
        + r"""
};
struct Converter {
 bool process_multi(const int16_t *in,size_t n,uint8_t stride,const uint8_t *slots,int16_t*,int16_t *mic,int16_t *ref,uint8_t){
 for(size_t i=0;i<n;i++){mic[i]=in[i*stride+slots[0]];ref[i]=in[i*stride+slots[1]];}return true;
 }
 bool process_multi_32(const int32_t*,size_t,uint8_t,const uint8_t*,int16_t*,int16_t*,int16_t*,uint8_t){assert(false);return false;}
};
struct ESPAudioStack {
 bool mic_channel_right_=false;uint32_t sample_rate_=48000;Converter rx_rate_converter_;std::vector<int16_t> selected;
 void fail_audio_session_(const char*){assert(false);}
 bool process_rx_stereo_ref_(AudioTaskCtx&);void capture_raw_audio_(const AudioTaskCtx&);
 void capture_audio_(CapturePoint,const void *data,size_t n,uint32_t,uint8_t,uint8_t channels,uint8_t stride,uint8_t first,uint8_t){
  assert(channels==1);auto *pcm=static_cast<const int16_t*>(data);
  for(size_t i=0;i<n;i++)selected.push_back(pcm[i*stride+first]);
 }
};
"""
    )
    code = '#include "audio_slot_layout.h"\n' + code
    code += method(capture, "void ESPAudioStack::capture_raw_audio_(") + "\n"
    code += method(pipeline, "bool ESPAudioStack::process_rx_stereo_ref_(") + "\n"
    code += f"\nint main(){{ESPAudioStack s;AudioTaskCtx c;c.ref_channel_right={str(reference_right).lower()};c.rx_slot_mode_stereo={str(stereo_slot_mode).lower()};\n"
    if has_layout:
        code += "c.rx_layout=AudioRxLayout::standard(c.rx_slot_mode_stereo,false,c.use_stereo_aec_ref,c.ref_channel_right,0,-1);\n"
    code += r"""
 int16_t pcm[]={101,901,102,902,103,903},mic[3]{},ref[3]{};c.rx_buffer=pcm;c.mic_buffer=mic;c.spk_ref_buffer=ref;
 assert(s.process_rx_stereo_ref_(c));s.capture_raw_audio_(c);
 assert((s.selected==std::vector<int16_t>(mic,mic+3)) && "diagnostic raw mic must use processing channel map");
 assert((s.selected==std::vector<int16_t>(c.ref_channel_right?std::initializer_list<int16_t>{101,102,103}:std::initializer_list<int16_t>{901,902,903})));
}
"""
    run_cpp(tmp_path, code, "-I" + str(COMPONENT))


def test_failed_rx_read_cannot_redispatch_prior_frame(tmp_path):
    source = (COMPONENT / "audio_pipeline.cpp").read_text()
    code = (
        r"""
#include <cassert>
#include <cstdint>
struct AudioTaskCtx {int16_t storage[4]{},*output_buffer=storage,*processor_input=storage,*mic_buffer=storage;bool mic_running=true;};
struct ESPAudioStack {
 void *rx_handle_=this;
 bool read_rx_frame_(AudioTaskCtx&,const char*){return false;}
 bool convert_rx_frame_(AudioTaskCtx&){assert(false);return true;}
 void apply_input_conditioning_(AudioTaskCtx&){assert(false);}
 void process_rx_path_(AudioTaskCtx&);bool can_dispatch_mic_frame_(const AudioTaskCtx&)const;
};
"""
        + method(source, "void ESPAudioStack::process_rx_path_(")
        + "\n"
        + method(source, "bool ESPAudioStack::can_dispatch_mic_frame_(")
        + r"""
int main(){ESPAudioStack s;AudioTaskCtx c;assert(s.can_dispatch_mic_frame_(c));s.process_rx_path_(c);
 assert(!s.can_dispatch_mic_frame_(c)&&c.processor_input==nullptr);}
"""
    )
    run_cpp(tmp_path, code)


def test_raw_layout_keeps_packed_tdm_and_std_microphone_identity(tmp_path):
    code = r"""
#include <cassert>
#include "audio_slot_layout.h"
using namespace esphome::esp_audio_stack;
int main(){
 auto mono=AudioRxLayout::standard(false,true,false,false,0,-1);
 assert(mono.stride==1&&mono.mic_slots[0]==0&&mono.mic_channels==1);
 auto std=AudioRxLayout::standard(true,false,false,false,1,0);
 assert(std.stride==2&&std.mic_slots[0]==1&&std.mic_slots[1]==0&&std.mic_channels==2);
 auto tdm=AudioRxLayout::tdm(AudioSlotLayout{0x8d},3,0,true,2);
 assert(tdm.stride==4&&tdm.mic_slots[0]==2&&tdm.mic_slots[1]==0&&tdm.reference_slot==1&&tdm.mic_channels==2);
 auto packed=AudioRxLayout::tdm(AudioSlotLayout{5},2,-1,true,0);
 assert(packed.stride==2&&packed.mic_slots[0]==1&&packed.reference_slot==0&&packed.mic_channels==1);
}
"""
    run_cpp(tmp_path, code, "-I" + str(COMPONENT))
