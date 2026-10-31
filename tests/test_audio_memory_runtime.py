"""Memory optimizations preserve public bounds and cold-owner allocation contracts."""
from pathlib import Path
import subprocess

import pytest

from test_volume_reference_runtime import method

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "esphome/components/esp_audio_stack"


def compile_run(tmp_path, source, *flags):
    cpp = tmp_path / "memory.cpp"
    cpp.write_text(source)
    exe = tmp_path / "memory"
    result = subprocess.run(["g++", "-std=c++20", "-O1", "-fsanitize=address,undefined",
                             *flags, str(cpp), "-o", str(exe)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    result = subprocess.run([str(exe)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_callback_reserve_uses_lifetime_bound_and_keeps_unknown_processor_fallback(tmp_path):
    pipeline = (COMPONENT / "audio_pipeline.cpp").read_text()
    code = r"""
#include <algorithm>
#include <cassert>
#include <cstddef>
#include <cstdint>
#define USE_AUDIO_PROCESSOR
constexpr size_t DEFAULT_AUDIO_FRAME_SAMPLES=256;
struct Spec {size_t input_samples=0,output_samples=0;};
struct Processor {size_t maximum=0,output=0;size_t maximum_output_samples()const{return maximum;}Spec frame_spec()const{return {output,output};}};
struct ESPAudioStack {Processor *processor_=nullptr;size_t get_mic_callback_buffer_size()const;};
""" + method(pipeline, "size_t ESPAudioStack::get_mic_callback_buffer_size() const") + r"""
int main(){
 ESPAudioStack s;assert(s.get_mic_callback_buffer_size()==512);
 Processor p;s.processor_=&p;
 assert(s.get_mic_callback_buffer_size()==2048); // late/custom processor
 p.maximum=256;assert(s.get_mic_callback_buffer_size()==512); // known before setup
 p.output=256;assert(s.get_mic_callback_buffer_size()==512);
 p.maximum=0;p.output=2048;assert(s.get_mic_callback_buffer_size()==4096);
 p.maximum=1024;p.output=512;assert(s.get_mic_callback_buffer_size()==2048);
}
"""
    compile_run(tmp_path, code)


@pytest.mark.parametrize("downmix", [False, True])
def test_working_buffers_follow_direction_and_mono_delivery_without_hot_growth(tmp_path, downmix):
    pipeline = (COMPONENT / "audio_pipeline.cpp").read_text()
    helpers = pipeline[pipeline.index("static constexpr size_t AUDIO_BUFFER_ALIGN"):
                       pipeline.index("size_t ESPAudioStack::get_mic_callback_buffer_size")]
    code = r"""
#include <algorithm>
#include <cassert>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <map>
#include <cstring>
#define USE_AUDIO_PROCESSOR
#define ESP_LOGI(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
constexpr unsigned MALLOC_CAP_SPIRAM=1,MALLOC_CAP_INTERNAL=2,MALLOC_CAP_8BIT=4;
std::map<void*,size_t> allocations;unsigned allocation_calls=0;
void *heap_caps_malloc(size_t n,unsigned){++allocation_calls;auto *p=malloc(n);if(p)allocations[p]=n;return p;}
void *heap_caps_aligned_alloc(size_t a,size_t n,unsigned){++allocation_calls;void *p=nullptr;if(posix_memalign(&p,a,n))return nullptr;allocations[p]=n;return p;}
void heap_caps_free(void*p){if(p){assert(allocations.erase(p));free(p);}}
size_t live_bytes(){size_t n=0;for(auto [p,s]:allocations)n+=s;return n;}
struct AudioTaskCtx {
 bool processor_spec_loaded=true,mic_separate=true,use_tdm_bus=true,use_stereo_aec_ref=false,use_tdm_ref=true,rx_slot_mode_stereo=false;
 unsigned speaker_channels=1,num_ch=1,ratio=1,i2s_bps=2;
 size_t rx_frame_bytes=1536,input_frame_bytes=512,speaker_frame_bytes=512,bus_frame_bytes=512,output_frame_bytes=512,bus_frame_size=256;
};
struct ESPAudioStack {
 bool buffers_in_psram_=true,audio_buffers_allocated_=false,rx=true;
 int tdm_second_mic_slot_=2;void *processor_=this;
 bool has_rx_input_()const{return rx;}
 int16_t *prealloc_rx_buffer_=nullptr,*prealloc_mic_buffer_=nullptr,*prealloc_processor_mic_buffer_=nullptr,
 *prealloc_spk_buffer_=nullptr,*prealloc_spk_ref_buffer_=nullptr,*prealloc_tx_ref_mono_buffer_=nullptr,*prealloc_aec_output_=nullptr;
 size_t prealloc_rx_buffer_bytes_=0,prealloc_mic_buffer_bytes_=0,prealloc_processor_mic_buffer_bytes_=0,
 prealloc_spk_buffer_bytes_=0,prealloc_spk_ref_buffer_bytes_=0,prealloc_tx_ref_mono_buffer_bytes_=0,prealloc_aec_output_bytes_=0;
 void release_audio_buffers_();bool allocate_audio_buffers_(AudioTaskCtx&);
};
""" + helpers
    code += method(pipeline, "void ESPAudioStack::release_audio_buffers_()")
    code += method(pipeline, "bool ESPAudioStack::allocate_audio_buffers_(")
    code += r"""
int main(){
 ESPAudioStack s;AudioTaskCtx c;
 assert(s.allocate_audio_buffers_(c));
#ifdef USE_ESP_AUDIO_STACK_MIC_DOWNMIX
 assert(s.prealloc_processor_mic_buffer_==nullptr);
#else
 assert(s.prealloc_processor_mic_buffer_bytes_==1024);
#endif
 const auto before=allocation_calls;const auto bytes=live_bytes();
 for(unsigned i=0;i<10;++i)assert(s.allocate_audio_buffers_(c));
 assert(allocation_calls==before&&live_bytes()==bytes);s.release_audio_buffers_();assert(allocations.empty());
 // Output-only hardware keeps its TX workspace but allocates no absent RX.
 s.rx=false;s.processor_=nullptr;s.tdm_second_mic_slot_=-1;c.processor_spec_loaded=false;
 c.use_tdm_ref=false;c.use_tdm_bus=false;c.rx_frame_bytes=512;
 assert(s.allocate_audio_buffers_(c));
 assert(!s.prealloc_rx_buffer_&&!s.prealloc_mic_buffer_&&s.prealloc_spk_buffer_);
 assert(live_bytes()==512);s.release_audio_buffers_();assert(allocations.empty());
}
"""
    compile_run(tmp_path, code, *(["-DUSE_ESP_AUDIO_STACK_MIC_DOWNMIX"] if downmix else []))


@pytest.mark.parametrize("enabled", [False, True])
def test_disabled_afe_counters_take_no_instance_storage_and_keep_telemetry_defaults(tmp_path, enabled):
    stub = tmp_path / "esphome/core/log.h"
    stub.parent.mkdir(parents=True)
    stub.write_text("#pragma once\n#define ESPHOME_LOG_LEVEL_DEBUG 4\n#define ESPHOME_LOG_LEVEL 4\n")
    header = ROOT / "esphome/components/esp_afe/afe_diagnostic_counter.h"
    code = '#include "' + str(header) + '"\n' + r"""
#include <cassert>
using namespace esphome::esp_afe;
struct Storage {
 unsigned live=7;
 [[no_unique_address]] AfeDiagnosticCounter<uint32_t,1> first{0};
 [[no_unique_address]] AfeDiagnosticCounter<uint32_t,2> second{0};
 [[no_unique_address]] AfeDiagnosticCounter<float,3,1> free{1.0f};
};
int main(){
 Storage s;s.first.store(9);s.second.fetch_add(3);s.free.store(0.25f);
#ifdef USE_ESP_AUDIO_STACK_TELEMETRY
 static_assert(sizeof(Storage)==16);assert(s.first.load()==9&&s.second.load()==3&&s.free.load()==0.25f);
#else
 static_assert(sizeof(Storage)==4);assert(s.first.load()==0&&s.second.load()==0&&s.free.load()==1.0f);
#endif
 assert(s.live==7);
}
"""
    compile_run(tmp_path, code, "-I" + str(tmp_path), *(["-DUSE_ESP_AUDIO_STACK_TELEMETRY"] if enabled else []))


def test_rate_converter_reuses_same_format_and_reopens_on_reset_failure(tmp_path):
    source = (COMPONENT / "audio_effects_rate_converter.cpp").read_text()
    helpers = source[source.index("namespace {"):source.index("}  // namespace")+len("}  // namespace")]
    fixture = (ROOT / "tests/fixtures/rate_conversion.cpp").read_text()
    fixture = fixture.replace("int esp_ae_rate_cvt_open(", "unsigned opens=0,resets=0,closes=0;bool reset_failure=false;\nint esp_ae_rate_cvt_open(")
    fixture = fixture.replace("*h=new esp_ae_rate_cvt_cfg_t(*c);return 0;", "++opens;*h=new esp_ae_rate_cvt_cfg_t(*c);return 0;")
    fixture = fixture.replace("int esp_ae_rate_cvt_reset(esp_ae_rate_cvt_handle_t){return 0;}", "int esp_ae_rate_cvt_reset(esp_ae_rate_cvt_handle_t){++resets;return reset_failure?1:0;}")
    fixture = fixture.replace("void esp_ae_rate_cvt_close(esp_ae_rate_cvt_handle_t h){delete h;}", "void esp_ae_rate_cvt_close(esp_ae_rate_cvt_handle_t h){++closes;delete h;}")
    code = fixture + helpers + r"""
int main(){
 {RateCvtHandle c;c.init(3,48000,16000,3,3,1);assert(c.ready());assert(opens==1&&resets==0);
  for(unsigned i=0;i<20;++i){c.init(3,48000,16000,3,3,1);assert(c.ready());}
  assert(opens==1&&resets==20&&closes==0);
  c.init(2,32000,16000,3,3,1);assert(c.ready());assert(opens==2&&closes==1);
  reset_failure=true;c.init(2,32000,16000,3,3,1);assert(closes==2&&c.ready()&&opens==3);
  reset_failure=false;c.reset();assert(opens==3&&closes==2);
 }assert(closes==3);
}
"""
    compile_run(tmp_path, code)


def mono_converter_fixture():
    source = (COMPONENT / "audio_effects_rate_converter.cpp").read_text()
    helpers = source[source.index("namespace {"):source.index("}  // namespace") + len("}  // namespace")]
    start = source.index("class AudioEffectsRateConverterImpl {")
    mono = source[start:source.index("\n};", start) + len("\n};")]
    fixture = (ROOT / "tests/fixtures/rate_conversion.cpp").read_text()
    fixture = fixture.replace(
        "unsigned allocation_calls=0;",
        "unsigned allocation_calls=0,free_calls=0,rate_open_calls=0,rate_opens=0,rate_resets=0,rate_closes=0,"
        "bit_opens=0,bit_closes=0;bool rate_open_failure=false;",
    )
    fixture = fixture.replace(
        "void heap_caps_free(void*p){allocations.erase(p);std::free(p);}",
        "void heap_caps_free(void*p){if(p){++free_calls;assert(allocations.erase(p)==1);}std::free(p);}",
    )
    fixture = fixture.replace(
        "*h=new esp_ae_bit_cvt_cfg_t(*c);return 0;",
        "++bit_opens;*h=new esp_ae_bit_cvt_cfg_t(*c);return 0;",
    )
    fixture = fixture.replace(
        "void esp_ae_bit_cvt_close(esp_ae_bit_cvt_handle_t h){delete h;}",
        "void esp_ae_bit_cvt_close(esp_ae_bit_cvt_handle_t h){++bit_closes;delete h;}",
    )
    fixture = fixture.replace(
        "*h=new esp_ae_rate_cvt_cfg_t(*c);return 0;",
        "++rate_open_calls;if(rate_open_failure)return 1;++rate_opens;*h=new esp_ae_rate_cvt_cfg_t(*c);return 0;",
    )
    fixture = fixture.replace(
        "int esp_ae_rate_cvt_reset(esp_ae_rate_cvt_handle_t){return 0;}",
        "int esp_ae_rate_cvt_reset(esp_ae_rate_cvt_handle_t){++rate_resets;return 0;}",
    )
    fixture = fixture.replace(
        "void esp_ae_rate_cvt_close(esp_ae_rate_cvt_handle_t h){delete h;}",
        "void esp_ae_rate_cvt_close(esp_ae_rate_cvt_handle_t h){++rate_closes;delete h;}",
    )
    return fixture + helpers + "\n" + mono


@pytest.mark.parametrize("ratio", [1, 3, 6])
def test_mono_converter_reserves_only_32bit_scratch_and_retains_it_between_formats(tmp_path, ratio):
    # The Espressif stub checks wrapper routing and exact output samples, not DSP quality.
    code = mono_converter_fixture() + r"""
int main(){
 constexpr size_t ratio=TEST_RATIO,quantum=256,in_count=quantum*ratio;
 std::vector<int16_t> input16(in_count),out(quantum);
 std::vector<int32_t> input32(in_count);
 for(size_t i=0;i<in_count;++i){
   input16[i]=static_cast<int16_t>(static_cast<int>(i*193%65536)-32768);
   input32[i]=static_cast<int32_t>(input16[i])*65536+static_cast<int32_t>(i%65536);
 }
 const auto expected_rate_opens=ratio>1?1u:0u;
 {
   AudioEffectsRateConverterImpl c;c.init(ratio,16000*ratio,16000,3,1);
   assert(c.prepare(in_count,false));
   assert(allocation_calls==0&&live_bytes()==0); // 16-bit samples go directly to the rate converter.
   assert(rate_open_calls==expected_rate_opens&&rate_opens==expected_rate_opens&&bit_opens==0);
   auto check_output=[&]{for(size_t i=0;i<quantum;++i)assert(out[i]==input16[i*ratio]);};
   for(unsigned frame=0;frame<128;++frame){
     assert(c.process(input16.data(),out.data(),in_count));check_output();
     assert(allocation_calls==0&&rate_opens==expected_rate_opens);
   }
   assert(c.prepare(in_count,true));
   // 48 -> 16 kHz with a 256-sample output quantum requires exactly 1536 scratch bytes.
   const auto expected_scratch=ratio>1?in_count*sizeof(int16_t):0;
   assert(live_bytes()==expected_scratch&&allocation_calls==(ratio>1?1u:0u));
   assert(rate_open_calls==expected_rate_opens&&rate_opens==expected_rate_opens&&bit_opens==1);
   const auto warm_allocations=allocation_calls;
   for(unsigned frame=0;frame<128;++frame){
     assert(c.process_strided_32(input32.data(),out.data(),quantum,1,0));check_output();
   }
   assert(c.prepare(in_count,false));
   for(unsigned frame=0;frame<128;++frame){
     assert(c.process_strided(input16.data(),out.data(),quantum,1,0));check_output();
   }
   assert(c.prepare(in_count,true));
   for(unsigned frame=0;frame<128;++frame){
     assert(c.process_strided_32(input32.data(),out.data(),quantum,1,0));check_output();
   }
   assert(live_bytes()==expected_scratch&&allocation_calls==warm_allocations&&free_calls==0);
   assert(rate_open_calls==expected_rate_opens&&rate_opens==expected_rate_opens&&rate_resets==0&&rate_closes==0);
   assert(bit_opens==1&&bit_closes==0);
 }
 assert(allocations.empty()&&free_calls==(ratio>1?1u:0u));
 assert(rate_closes==expected_rate_opens&&bit_closes==1);
}
"""
    compile_run(tmp_path, code, f"-DTEST_RATIO={ratio}")


def test_mono_16bit_prepare_still_reports_rate_converter_open_failure(tmp_path):
    code = mono_converter_fixture() + r"""
int main(){
 AudioEffectsRateConverterImpl c;c.init(3,48000,16000,3,1);
 rate_open_failure=true;
 assert(!c.prepare(768,false));
 assert(rate_open_calls==1&&rate_opens==0&&bit_opens==0);
 assert(allocation_calls==0&&allocations.empty());
}
"""
    compile_run(tmp_path, code)


@pytest.mark.parametrize("dual", [False, True])
def test_clock_only_capture_omits_playback_ring_but_direct_cpp_output_keeps_it(tmp_path, dual):
    owner = (COMPONENT / "esp_audio_stack.cpp").read_text()
    header = (COMPONENT / "esp_audio_stack.h").read_text()
    ring = method(owner, "  if (this->has_tx_output_())")
    code = r"""
#include <algorithm>
#include <cassert>
#include <cstdint>
#include <memory>
constexpr size_t SPEAKER_BUFFER_MIN_BYTES=2048;
#define ESP_LOGE(...) ((void)0)
size_t ring_bytes=0;unsigned calls=0;
std::shared_ptr<int> create_prefer_psram(size_t n,const char*){++calls;ring_bytes=n;return std::make_shared<int>(1);}
struct ESPAudioStack {
 int din_pin_=4,dout_pin_=-1;bool dual_i2s_bus_=false;
 struct Bus{int din_pin=-1,dout_pin=-1;}rx_bus_,tx_bus_;
 unsigned sample_rate_=16000,speaker_buffer_duration_ms_=500;
 size_t speaker_buffer_size_=0;std::shared_ptr<int>speaker_buffer_;
 unsigned get_speaker_channels()const{return 1;}void mark_failed(){assert(false);}
""" + method(header, "  bool has_rx_input_() const") + method(header, "  bool has_tx_output_() const")
    code += "void setup_ring(){" + ring + "}\n};\n"
    code += r"""
int main(){
 ESPAudioStack s;
#ifdef USE_ESP_AUDIO_STACK_DUAL_BUS
 s.dual_i2s_bus_=true;s.rx_bus_.din_pin=4;
#endif
 assert(s.has_rx_input_());s.setup_ring();assert(calls==0&&!s.speaker_buffer_&&s.speaker_buffer_size_==0);
#ifdef USE_ESP_AUDIO_STACK_DUAL_BUS
 s.tx_bus_.dout_pin=8;
#else
 s.dout_pin_=8;
#endif
 // No facade is necessary for advanced C++ play() callers with physical TX.
 s.setup_ring();assert(calls==1&&s.speaker_buffer_&&ring_bytes==16000);
 s.sample_rate_=48000;s.setup_ring();assert(ring_bytes==48000);
}
"""
    compile_run(tmp_path, code, *(["-DUSE_ESP_AUDIO_STACK_DUAL_BUS"] if dual else []))
