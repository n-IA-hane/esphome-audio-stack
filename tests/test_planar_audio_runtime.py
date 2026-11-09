"""Differential production conversion, conditioning and AFE layout assembly.

DSP primitives are adapters here. The separate on-device private self-test
qualifies the actual Espressif resampler binaries before a dependency change.
"""
from pathlib import Path
import subprocess
import pytest

from test_volume_reference_runtime import method

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "esphome/components/esp_audio_stack"


def test_planar_matches_interleaved_without_reweaving_or_hot_allocation(tmp_path):
    converter = (COMPONENT / "audio_effects_rate_converter.cpp").read_text()
    helpers = converter[converter.index("namespace {"):converter.index("}  // namespace") + len("}  // namespace")]
    component = method(converter, "class MultiChannelAudioEffectsRateConverterImpl") + ";\n"
    pipeline = (COMPONENT / "audio_pipeline.cpp").read_text()
    afe = (ROOT / "esphome/components/esp_afe/esp_afe.cpp").read_text()
    staging = method(afe, "static inline void stage_afe_input_frame(")
    fixture = (ROOT / "tests/fixtures/rate_conversion.cpp").read_text().replace(
        "int esp_ae_intlv_process(", "unsigned interleaves=0;\nint esp_ae_intlv_process(",
    ).replace("  assert(bits==16);for(size_t i=0;i<n;++i)for(size_t c=0;c<channels;++c)static_cast<int16_t*>(out)[i*channels+c]",
              "  ++interleaves;assert(bits==16);for(size_t i=0;i<n;++i)for(size_t c=0;c<channels;++c)static_cast<int16_t*>(out)[i*channels+c]")
    planar = "bool process_planar(" in component
    program = fixture + f'\n#include "{COMPONENT / "audio_core_audio_utils.h"}"\n' + r'''
using namespace esphome::esp_audio_stack;
namespace esp_audio_libs::gain {
void apply(void*src,void*dst,int32_t gain,size_t samples,size_t){
 auto*in=static_cast<int16_t*>(src);auto*out=static_cast<int16_t*>(dst);
 for(size_t i=0;i<samples;i++)out[i]=int16_t((int64_t(in[i])*gain)>>31);
}
}
struct AudioTaskCtx {
 int16_t*mic_buffer=nullptr,*processor_mic_buffer=nullptr;
 size_t input_frame_size=0;uint8_t processor_mic_channels=2;
 bool processor_planar_input=false,correct_dc_offset=false;
 int32_t input_gain_q31=INT32_MAX;float input_gain_boost=1;
};
struct ESPAudioStack {
 DcBlockerState dc_primary_,dc_secondary_;
 void apply_input_conditioning_(AudioTaskCtx&);
};
struct afe_pcm_config_t{int total_ch_num,mic_num;uint8_t*mic_ids;int ref_num;uint8_t*ref_ids;};
''' + helpers + "\n" + component + method(pipeline, "void ESPAudioStack::apply_input_conditioning_(") + staging
    program += r'''
int main(){
 constexpr size_t count=256;uint8_t offsets[]={2,0,1},mic_ids[]={1,2},ref_ids[]={0};
 afe_pcm_config_t layout{4,2,mic_ids,1,ref_ids};
 for(unsigned bits:{16u,32u})for(unsigned ratio:{1u,2u,3u})for(unsigned gain:{0u,1u,2u,3u}){
  std::vector<int16_t> raw(count*ratio*4),mic[2],other[2],ref[2],observed[2];
  std::vector<int32_t> wide(raw.size());
  for(size_t i=0;i<raw.size();i++){raw[i]=int16_t(700+((i*7)%1700));wide[i]=int32_t(raw[i])*65536+17;}
  size_t memory[2]{};
  for(unsigned mode=0;mode<2;mode++){
   mic[mode].assign(count+2,12345);other[mode].assign(count*2+2,23456);ref[mode].assign(count+2,3456);
   MultiChannelAudioEffectsRateConverterImpl cvt;cvt.init(ratio,3,16000*ratio,16000,3,1);
   int16_t*primary=mic[mode].data()+1,*secondary=other[mode].data()+1,*reference=ref[mode].data()+1;
// PREPARE
   memory[mode]=live_bytes();
   const unsigned allocations_before=allocation_calls,interleaves_before=interleaves;
   ESPAudioStack stack;AudioTaskCtx ctx;ctx.mic_buffer=primary;ctx.processor_mic_buffer=secondary;ctx.input_frame_size=count;
// PLANAR_CONTEXT
   ctx.correct_dc_offset=gain==3;ctx.input_gain_boost=gain==2?1.75f:1;
   ctx.input_gain_q31=gain==0?0:gain==1?INT32_MAX/2:INT32_MAX;
   for(unsigned period=0;period<3;period++){
// CONVERT
    stack.apply_input_conditioning_(ctx);
    std::vector<int16_t> staged(count*4+2,13579);
// STAGE
    assert(staged.front()==13579&&staged.back()==13579);
    observed[mode].insert(observed[mode].end(),staged.begin()+1,staged.end()-1);
   }
   assert(allocation_calls==allocations_before&&"all converter storage must be prepared before PCM");
   if(mode)assert(interleaves==interleaves_before&&"planar path must not reweave channels before AFE staging");
   assert(mic[mode].front()==12345&&mic[mode].back()==12345&&other[mode].front()==23456&&other[mode].back()==23456);
  }
  assert(observed[0]==observed[1]&&"planar conversion/conditioning must preserve every AFE role and padding sample");
  assert(mic[0]==mic[1]&&ref[0]==ref[1]);
  assert(memory[0]==memory[1]+(ratio>1?count*sizeof(int16_t):0)&&"secondary plane must borrow the prepared destination");
 }
 assert(allocations.empty());
}
'''
    prepare = "assert(cvt.prepare(count*ratio,count,3,4,bits==32,primary,reference,2" + (",mode?secondary:nullptr" if planar else "") + "));"
    interleaved = "bits==32?cvt.process_multi_32(wide.data(),count,4,offsets,secondary,primary,reference,2):cvt.process_multi(raw.data(),count,4,offsets,secondary,primary,reference,2)"
    planar_call = "bits==32?cvt.process_planar_32(wide.data(),count,4,offsets,primary,secondary,reference,2):cvt.process_planar(raw.data(),count,4,offsets,primary,secondary,reference,2)" if planar else interleaved
    convert = f"bool ok=mode?({planar_call}):({interleaved});assert(ok);"
    stage = "stage_afe_input_frame(staged.data()+1," + ("mode?primary:secondary" if planar else "secondary") + ",reference,count,2,layout" + (",mode?secondary:nullptr" if "in_secondary" in staging else "") + ");"
    program = program.replace("// PREPARE", prepare).replace("// CONVERT", convert).replace("// STAGE", stage).replace(
        "// PLANAR_CONTEXT", "ctx.processor_planar_input=mode;" if planar else "",
    )
    cpp = tmp_path / "planar.cpp"
    cpp.write_text(program)
    binary = tmp_path / "planar"
    built = subprocess.run(["g++", "-std=c++20", "-O1", "-g", "-fsanitize=address,undefined",
                            str(cpp), "-o", str(binary)], capture_output=True, text=True)
    assert built.returncode == 0, built.stderr
    run = subprocess.run([str(binary)], capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr


@pytest.mark.parametrize("diagnostic", [None, "USE_ESP_AUDIO_STACK_CAPTURE", "USE_ESP_AUDIO_STACK_TEST_INPUT"])
def test_planar_is_optional_and_diagnostic_builds_preserve_interleaved_contract(tmp_path, diagnostic):
    pipeline = (COMPONENT / "audio_pipeline.cpp").read_text()
    start = pipeline.index("      // Capture views and the writable test hook")
    gate = pipeline[start:pipeline.index("#endif", start) + len("#endif")]
    source = f'#include "{COMPONENT / "audio_core_processor.h"}"\n' + r'''
#include <cassert>
using namespace esphome::esp_audio_stack;
struct Legacy:AudioProcessor {
 unsigned calls=0;
 bool is_initialized()const override{return true;}
 FrameSpec frame_spec()const override{return {};}
 bool process(const int16_t*,const int16_t*,int16_t*,uint8_t)override{++calls;return true;}
 FeatureControl feature_control(AudioFeature)const override{return FeatureControl::NOT_SUPPORTED;}
 bool set_feature(AudioFeature,bool)override{return false;}
 ProcessorTelemetry telemetry()const override{return {};}
 bool reconfigure(int,int)override{return false;}
};
int main(){
 Legacy legacy;assert(!legacy.frame_spec().supports_planar_input);
 FrameSpec historical{16000,2,1,256,256};assert(!historical.supports_planar_input);
 const int16_t mono[4]{},*planes[]={mono,mono};int16_t output[4]{};
 assert(legacy.process_planar(planes,nullptr,output,1)&&legacy.calls==1);
 assert(!legacy.process_planar(planes,nullptr,output,2)&&legacy.calls==1);
 struct {bool processor_planar_input=false;} ctx;
 FrameSpec spec;spec.supports_planar_input=true;
''' + gate + f"\n assert(ctx.processor_planar_input=={str(diagnostic is None).lower()});\n}}\n"
    cpp = tmp_path / "capability.cpp"
    cpp.write_text(source)
    binary = tmp_path / "capability"
    flags = ["-D" + diagnostic] if diagnostic else []
    built = subprocess.run(["g++", "-std=c++17", *flags, str(cpp), "-o", str(binary)], capture_output=True, text=True)
    assert built.returncode == 0, built.stderr
    subprocess.run([str(binary)], check=True)
