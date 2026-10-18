"""Shared volume/reference plumbing, independent of the selected DSP processor.

Compile production methods with transport/codec adapters. This checks gain
ownership and reference delivery, not AEC/AFE acoustic performance.
"""
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1] / "esphome/components/esp_audio_stack"


def method(text, signature):
    start = text.index(signature)
    end = text.index("{", start) + 1
    depth = 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


@pytest.mark.parametrize("hardware_master", [False, True])
def test_master_gain_has_one_owner_and_reference_is_not_scaled_twice(tmp_path, hardware_master):
    stack = (ROOT / "esp_audio_stack.cpp").read_text()
    pipeline = (ROOT / "audio_pipeline.cpp").read_text()
    code = r'''
#include <atomic>
#include <cassert>
#include <cmath>
#include <cstdint>
#include <cstring>
#define ESP_LOGD(...) ((void)0)
#define USE_ESP_AUDIO_STACK_PREVIOUS_FRAME_REF
struct Codec { float master=-1; void set_output_volume(float value){master=value;} };
struct Processor {
 const int16_t *reference=nullptr;
 void process(const int16_t*,const int16_t *ref,int16_t*,uint8_t){reference=ref;}
};
struct AudioTaskCtx {
 const int16_t *processor_input=nullptr;
 int16_t *spk_ref_buffer=nullptr,*aec_output=nullptr,*output_buffer=nullptr;
 uint8_t processor_mic_channels=1;
 size_t input_frame_bytes=8,output_frame_size=4,output_frame_bytes=8;
 size_t current_output_frame_size=0,current_output_frame_bytes=0;
};
struct ESPAudioStack {
 std::atomic<int32_t> output_volume_q31_{0},master_volume_q31_{0},hot_output_volume_q31_{0};
 std::atomic<float> master_volume_linear_{0},master_volume_public_{0};
 Codec codec_backend_; Processor *processor_=nullptr;
 int16_t *direct_aec_ref_=nullptr;bool direct_aec_ref_valid_=false;
 void update_hot_output_volume_();
 void fill_mono_aec_reference_(AudioTaskCtx&);
 void run_processor_(AudioTaskCtx&);
};
'''
    code += method(stack, "[[maybe_unused]] int32_t multiply_q31(") + "\n"
    code += method(stack, "void ESPAudioStack::update_hot_output_volume_(") + "\n"
    code += method(pipeline, "void ESPAudioStack::fill_mono_aec_reference_(") + "\n"
    code += method(pipeline, "void ESPAudioStack::run_processor_(") + "\n"
    code += r'''
int main(){
 ESPAudioStack stack;
 stack.master_volume_q31_=1<<30;stack.master_volume_linear_=.75f;
 stack.output_volume_q31_=1<<29;
 stack.update_hot_output_volume_();
#ifdef USE_ESP_AUDIO_STACK_HARDWARE_OUTPUT_CODEC
 assert(stack.hot_output_volume_q31_==(1<<29));
 assert(stack.codec_backend_.master==.75f);
#else
 assert(stack.hot_output_volume_q31_==(1<<28));
 assert(stack.codec_backend_.master==-1);
 stack.master_volume_q31_=0;stack.update_hot_output_volume_();
 assert(stack.hot_output_volume_q31_==0);
#endif
 // Reference has already passed TX gain and rate conversion. Neither delivery
 // nor the common processor dispatch may apply the volume a second time.
 int16_t post_volume[4]={1000,-1000,327,-327},reference[4]{},output[4]{};
 stack.direct_aec_ref_=post_volume;stack.direct_aec_ref_valid_=true;
 AudioTaskCtx ctx;ctx.spk_ref_buffer=reference;ctx.aec_output=output;
 Processor selected_processor;stack.processor_=&selected_processor;
 stack.fill_mono_aec_reference_(ctx);
 stack.run_processor_(ctx);
 assert(std::memcmp(reference,post_volume,sizeof(reference))==0);
 assert(selected_processor.reference==reference && ctx.output_buffer==output);
 stack.direct_aec_ref_valid_=false;stack.fill_mono_aec_reference_(ctx);
 for(auto sample:reference)assert(sample==0);
}
'''
    source = tmp_path / "volume_reference.cpp"
    source.write_text(code)
    binary = tmp_path / "volume_reference"
    flags = ["-DUSE_ESP_AUDIO_STACK_HARDWARE_OUTPUT_CODEC", "-DUSE_ESP_AUDIO_STACK_HARDWARE_CODEC"] if hardware_master else []
    subprocess.run(["g++", "-std=c++17", *flags, str(source), "-o", str(binary)],
                   check=True, capture_output=True, text=True)
    result = subprocess.run([str(binary)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_software_reference_is_taken_after_tx_volume_and_before_processor_dispatch():
    pipeline = (ROOT / "audio_pipeline.cpp").read_text()
    tx = method(pipeline, "void ESPAudioStack::process_tx_path_(")
    assert tx.index("esp_audio_libs::gain::apply(") < tx.index("const int16_t *ref_source = ctx.spk_buffer")
    assert tx.index("const int16_t *ref_source = ctx.spk_buffer") < tx.index("play_ref_rate_converter_.process(ref_source")
    dispatch = method(pipeline, "void ESPAudioStack::process_aec_and_callbacks_(")
    reference = dispatch.index("this->fill_mono_aec_reference_(ctx)")
    assert reference < dispatch.index("this->run_processor_(ctx)", reference)
