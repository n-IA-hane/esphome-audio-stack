"""Shared volume/reference plumbing, independent of the selected DSP processor.

Compile production methods with transport/codec adapters. This checks gain
ownership and reference delivery, not AEC/AFE acoustic performance.
"""
from pathlib import Path
import re
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
def test_master_number_bootstrap_reports_requested_level_not_effective_gain(tmp_path, hardware_master):
    header = (ROOT / "esp_audio_stack.h").read_text()
    number = (ROOT / "number.h").read_text().split("class MasterVolumeNumber", 1)[1]
    setup = method(number, "void setup()")
    getter_name = re.search(r"parent_->(get_master_volume\w*)\(\) \* 100", setup)[1]
    getter = method(header, "float " + getter_name + "()")
    cpp = tmp_path / "master_level.cpp"
    cpp.write_text(r'''
#include <atomic>
#include <cassert>
#include <cmath>
struct ESPAudioStack {
 std::atomic<float> master_volume_linear_{.5f};
 std::atomic<float> master_volume_public_{0};
''' + getter + r'''
};
int main(){
 ESPAudioStack stack;
 // Independent requested UI level and applied gain for the unchanged -49 dB curve.
 const float applied=std::pow(10.f,(-49.f+49.f*.5f)/20.f);
 stack.master_volume_public_=HARDWARE ? .5f : applied;
 const float published=stack.''' + getter_name + r'''()*100.f;
 assert(std::abs(published-50.f)<.0001f);
 // Reading the logical control cannot rewrite either applied DSP or codec state.
 assert(std::abs(stack.master_volume_public_.load()-(HARDWARE ? .5f : applied))<.0001f);
}
''')
    binary = tmp_path / "master_level"
    subprocess.run(["g++", "-std=c++17", f"-DHARDWARE={int(hardware_master)}", str(cpp), "-o", str(binary)], check=True)
    result = subprocess.run([str(binary)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


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
#include <thread>
void xTaskNotifyGive(void*){}
#define ESP_LOGD(...) ((void)0)
#define USE_ESP_AUDIO_STACK_PREVIOUS_FRAME_REF
struct Codec { float master=-1; void set_output_volume(float value){master=value;} void set_output_volume_curve(float){} };
struct Processor {
 const int16_t *reference=nullptr;
 void process(const int16_t*,const int16_t *ref,int16_t*,uint8_t){reference=ref;}
 void process_planar(const int16_t*const*,const int16_t*ref,int16_t*,uint8_t){reference=ref;}
};
struct AudioTaskCtx {
 const int16_t *processor_input=nullptr;
 int16_t *spk_ref_buffer=nullptr,*aec_output=nullptr,*output_buffer=nullptr,*mic_buffer=nullptr,*processor_mic_buffer=nullptr;
 bool processor_planar_input=false;
 uint8_t processor_mic_channels=1;
 size_t input_frame_bytes=8,output_frame_size=4,output_frame_bytes=8;
 size_t current_output_frame_size=0,current_output_frame_bytes=0;
};
struct ESPAudioStack {
 std::atomic<int32_t> output_volume_q31_{0},master_volume_q31_{0},hot_output_volume_q31_{0};
 std::atomic<float> master_volume_linear_{0},master_volume_public_{0};
 std::atomic<bool> hardware_volume_dirty_{false},hardware_volume_curve_dirty_{true};
 std::atomic<float> master_volume_min_db_{-49};
 void *audio_task_handle_=nullptr;
 void apply_pending_hardware_volume_();
 Codec codec_backend_; Processor *processor_=nullptr;
 int16_t *direct_aec_ref_=nullptr;bool direct_aec_ref_valid_=false;
 void update_hot_output_volume_();
 void fill_mono_aec_reference_(AudioTaskCtx&);
 void run_processor_(AudioTaskCtx&);
};
'''
    code += method(stack, "[[maybe_unused]] int32_t multiply_q31(") + "\n"
    code += method(stack, "void ESPAudioStack::update_hot_output_volume_(") + "\n"
    code += method(stack, "void ESPAudioStack::apply_pending_hardware_volume_(") + "\n"
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
 assert(stack.codec_backend_.master==-1); // main loop never writes the codec
 std::thread worker([&]{stack.apply_pending_hardware_volume_();});worker.join();
 assert(stack.codec_backend_.master==.75f && !stack.hardware_volume_dirty_);
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
    subprocess.run(["g++", "-std=c++17", "-pthread", *flags, str(source), "-o", str(binary)],
                   check=True, capture_output=True, text=True)
    result = subprocess.run([str(binary)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_software_reference_is_taken_after_tx_volume_and_before_processor_dispatch():
    pipeline = (ROOT / "audio_pipeline.cpp").read_text()
    tx = method(pipeline, "void ESPAudioStack::process_tx_path_(")
    reference_owner = method(pipeline, "void ESPAudioStack::save_tx_reference_(")
    assert tx.index("esp_audio_libs::gain::apply(") < tx.index("write_tx_dma_blocks_(")
    assert tx.index("write_tx_dma_blocks_(") < tx.index("save_tx_reference_(ctx, written ?")
    assert "play_ref_rate_converter_.process(samples" in reference_owner
    dispatch = method(pipeline, "void ESPAudioStack::process_aec_and_callbacks_(")
    reference = dispatch.index("this->fill_mono_aec_reference_(ctx)")
    assert reference < dispatch.index("this->run_processor_(ctx)", reference)


def test_volume_only_updates_do_not_reallocate_codec_curve(tmp_path):
    source = (ROOT / 'esp_audio_stack.cpp').read_text()
    code = r'''
#include <cassert>
#include <atomic>
#define USE_ESP_AUDIO_STACK_HARDWARE_CODEC
struct Codec {
 unsigned curves=0,volumes=0;bool fail=false;float applied=0;
 void set_output_volume_curve(float value){++curves;if(!fail)applied=value;}
 void set_output_volume(float){++volumes;}
};
struct ESPAudioStack {
 std::atomic<bool> hardware_volume_dirty_{true},hardware_volume_curve_dirty_{true};
 std::atomic<float> master_volume_min_db_{-49},master_volume_public_{.5};
 Codec codec_backend_;
 void apply_pending_hardware_volume_();
};
''' + method(source, 'void ESPAudioStack::apply_pending_hardware_volume_(') + r'''
int main(){
 ESPAudioStack s;s.apply_pending_hardware_volume_();assert(s.codec_backend_.curves==1);
 for(unsigned i=0;i<100;++i){s.hardware_volume_dirty_=true;s.apply_pending_hardware_volume_();}
 assert(s.codec_backend_.curves==1 && s.codec_backend_.volumes==101);
 // A failed configuration must not prevent an explicit retry of the same value.
 s.master_volume_min_db_=-30;s.codec_backend_.fail=true;
 s.hardware_volume_curve_dirty_=true;s.hardware_volume_dirty_=true;s.apply_pending_hardware_volume_();
 assert(s.codec_backend_.applied==-49);
 s.codec_backend_.fail=false;s.hardware_volume_curve_dirty_=true;s.hardware_volume_dirty_=true;
 s.apply_pending_hardware_volume_();assert(s.codec_backend_.applied==-30 && s.codec_backend_.curves==3);
}
'''
    cpp = tmp_path / 'curve.cpp'
    exe = tmp_path / 'curve'
    cpp.write_text(code)
    subprocess.run(['g++', '-std=c++17', str(cpp), '-o', str(exe)], check=True, capture_output=True, text=True)
    subprocess.run([str(exe)], check=True, capture_output=True, text=True)
