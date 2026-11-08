"""Execute the gain owner and codec methods with deterministic I2C outcomes."""
from pathlib import Path
import os
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "esphome/components/esp_audio_stack"


def method(source, signature):
    start = source.index(signature)
    return source[start:source.index("\n}", start) + 2]


def compile_run(tmp_path, code):
    cpp = tmp_path / "gain.cpp"
    cpp.write_text(code)
    subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", str(cpp), "-o", str(tmp_path / "gain")], check=True, capture_output=True, text=True)
    subprocess.run([str(tmp_path / "gain")], check=True, capture_output=True, text=True)


@pytest.mark.parametrize("diagnostics", [False, True])
def test_gain_requests_run_only_on_the_codec_owner(tmp_path, diagnostics):
    source = (COMPONENT / "audio_codec_controls.cpp").read_text()
    source = source[source.index("namespace esphome::esp_audio_stack {"):source.rindex("#endif")]
    compile_run(tmp_path, ("#define USE_ESP_AUDIO_STACK_GAIN_DIAGNOSTICS\n" if diagnostics else "") + r'''
#include <atomic>
#include <cmath>
#include <cassert>
#include <cstdint>
#include <functional>
#define ESP_LOGW(...) ((void)0)
#define ESP_LOGI(...) ((void)0)
unsigned notifications=0;
void xTaskNotifyGive(void*){++notifications;}
namespace esphome::esp_audio_stack {
struct Backend {
 bool ok=true,opened=false;unsigned calls=0,read_calls=0;float gains[2]{};std::function<void()> during_write;
 bool write(unsigned index,float db){++calls;if(during_write){auto fn=during_write;during_write={};fn();}if(!ok)return false;gains[index]=db;return true;}
 bool set_input_gain(float db){return write(0,db);}
 bool set_input_channel_gain(unsigned ch,float db){assert(ch==2);return write(1,db);}
 float normalize_input_gain(float v)const{return !std::isfinite(v)||v<0||v>37.5?NAN:v;}
 bool is_open()const{return opened;}
 bool hardware_gain_readback_supported()const{return true;}
 struct Readback{float microphone,reference;};
 Readback read_hardware_gains(bool){++read_calls;return {ok?gains[0]:NAN,gains[1]};}
};
struct ESPAudioStack {
 enum class HardwareGainState:uint8_t{GAIN_CONFIGURED,GAIN_WRITE_CONFIRMED,GAIN_READ_BACK,GAIN_UNKNOWN};
 Backend codec_backend_;void *audio_task_handle_=nullptr;
 bool hardware_reference_gain_available_=false;uint8_t hardware_reference_gain_channel_=0;
 std::atomic<float> hardware_gain_requested_[2]{},hardware_gain_applied_[2]{};
 std::atomic<uint32_t> hardware_gain_revision_[2]{},hardware_gain_result_revision_[2]{};
 std::atomic<bool> hardware_gain_failed_[2]{},hardware_gain_result_pending_{false};
 std::atomic<HardwareGainState> hardware_gain_state_[2]{};
 std::atomic<uint8_t> hardware_gains_dirty_{0};unsigned loop_wakes=0;
 std::atomic<bool> hardware_gain_diagnostics_requested_{false};void request_hardware_gain_diagnostics();
 void enable_loop_soon_any_context(){++loop_wakes;}
 void configure_hardware_gain_defaults_(float,bool,uint8_t,float);
 bool request_hardware_gain(bool,float);bool hardware_gain_result(bool,float*,bool* = nullptr)const;
 void apply_pending_hardware_gains_();void refresh_hardware_gain_readback_(bool=false);
};
}
''' + source + r'''
int main(){using namespace esphome::esp_audio_stack;ESPAudioStack s;
 s.configure_hardware_gain_defaults_(12,true,2,24);
 assert(!s.request_hardware_gain(false,NAN)&&!s.request_hardware_gain(false,38));
 assert(s.request_hardware_gain(false,15));assert(!notifications&&!s.codec_backend_.calls);
 s.apply_pending_hardware_gains_();float result=0;bool persist=false;
 assert(s.hardware_gain_result(false,&result,&persist)&&result==15&&persist);
 assert(s.hardware_gain_state_[0]==ESPAudioStack::HardwareGainState::GAIN_CONFIGURED);
 s.codec_backend_.opened=true;s.audio_task_handle_=&s;
 assert(s.request_hardware_gain(true,27));assert(notifications==1);
 s.apply_pending_hardware_gains_();assert(s.hardware_gain_result(true,&result)&&result==27);
 assert(s.hardware_gain_state_[0]==ESPAudioStack::HardwareGainState::GAIN_READ_BACK);
 s.codec_backend_.ok=false;assert(s.request_hardware_gain(false,21));s.apply_pending_hardware_gains_();
 assert(s.hardware_gain_result(false,&result,&persist)&&std::isnan(result)&&!persist);
 assert(s.hardware_gain_failed_[0]);assert(s.hardware_gain_applied_[1]==27);
 // A newer request during an older write cannot be acknowledged or persisted.
 s.codec_backend_.ok=true;s.request_hardware_gain(false,18);
 s.codec_backend_.during_write=[&]{s.request_hardware_gain(false,30);};s.apply_pending_hardware_gains_();
 assert(!s.hardware_gain_result(false,&result));s.apply_pending_hardware_gains_();
 assert(s.hardware_gain_result(false,&result,&persist)&&result==30&&persist);
 ESPAudioStack no_ref;no_ref.configure_hardware_gain_defaults_(0,false,0,0);
 assert(!no_ref.request_hardware_gain(true,10));
#ifdef USE_ESP_AUDIO_STACK_GAIN_DIAGNOSTICS
 const auto writes=s.codec_backend_.calls,reads=s.codec_backend_.read_calls;
 s.request_hardware_gain_diagnostics();assert(s.codec_backend_.read_calls==reads);
 s.apply_pending_hardware_gains_();assert(s.codec_backend_.read_calls==reads+1&&s.codec_backend_.calls==writes);
 s.apply_pending_hardware_gains_();assert(s.codec_backend_.read_calls==reads+1);
#endif
}
''')


def backend_code():
    source = (COMPONENT / "codec_dev_backend.cpp").read_text()
    methods = ["float CodecDevBackend::normalize_input_gain(", "void CodecDevBackend::set_es7210_config(",
               "void CodecDevBackend::set_input_codec_config(", "bool CodecDevBackend::set_input_gain(",
               "bool CodecDevBackend::set_input_channel_gain(", "CodecDevBackend::GainReadback CodecDevBackend::read_hardware_gains("]
    return r'''
#include <cmath>
#include <cassert>
#include <cstdint>
#define ESP_CODEC_DEV_OK 0
#define ESP_CODEC_DEV_MAKE_CHANNEL_MASK(x) (1U<<(x))
unsigned calls=0,reads=0;int registers[256]{};int fail_channel=-1,fail_read=-1;
float vendor_quantize(float db){db+=.5f;if(db<33)return int(db/3)*3;return db<34.5?30:db<36?34.5:db<37?36:37.5;}
int code_for(float db){return db<=33?int(db/3):db==34.5?12:db==36?13:14;}
int esp_codec_dev_set_in_channel_gain(void*,uint16_t mask,float db){++calls;int ret=0;for(int c=0;c<4;c++)if(mask&(1U<<c)){if(c==fail_channel){ret=-1;continue;}registers[0x43+c]=code_for(vendor_quantize(db));}return ret;}
int esp_codec_dev_set_in_gain(void*,float db){++calls;registers[0x16]=int(db/6);return 0;}
int esp_codec_dev_read_reg(void*,int reg,int *value){++reads;if(reg==fail_read)return -1;*value=registers[reg];return 0;}
namespace esphome::esp_audio_stack {
struct CodecDevBackend {
 enum class CodecKind:uint8_t{NONE,ES8311};
 struct Es7210Config{bool enabled=false;uint8_t mic_selected=15;float input_gain_db=30;bool has_ref_channel_gain=true;uint8_t ref_channel=2;float ref_channel_gain_db=24;};
 struct GenericCodecConfig{bool enabled=true;CodecKind kind=CodecKind::ES8311;float input_gain_db=24;};
 struct GainReadback{float microphone,reference;};
 Es7210Config es7210_;GenericCodecConfig input_codec_;bool open_=false;void*rx_dev_=this;
 bool has_input_codec()const{return true;}
 float normalize_input_gain(float)const;void set_es7210_config(const Es7210Config&);void set_input_codec_config(const GenericCodecConfig&);
 bool set_input_gain(float,bool=false);bool set_input_channel_gain(uint8_t,float,bool=false);
 GainReadback read_hardware_gains(bool=false);
};
''' + "\n".join(method(source, m) for m in methods) + "\n}\n"


def test_codec_quantization_reference_partial_failure_and_reopen_cache(tmp_path):
    compile_run(tmp_path, backend_code() + r'''
int main(){using esphome::esp_audio_stack::CodecDevBackend;
 CodecDevBackend b;auto adc=b.es7210_;adc.enabled=true;adc.input_gain_db=20;adc.ref_channel_gain_db=37.5;b.set_es7210_config(adc);
 assert(b.es7210_.input_gain_db==18&&b.es7210_.ref_channel_gain_db==37.5);
 const float requested[]={0,2.5,20,20.5,21,32.5,33,34,35.5,36.5,37.5};
 const float expected[]={0,3,18,21,21,30,30,34.5,36,37.5,37.5};
 for(unsigned n=0;n<sizeof(requested)/sizeof(float);n++)assert(b.normalize_input_gain(requested[n])==expected[n]);
 assert(b.set_input_gain(20.5)&&calls==0&&b.es7210_.input_gain_db==21);
 b.open_=true;registers[0x45]=14;assert(b.set_input_gain(20));
 assert(registers[0x43]==6&&registers[0x44]==6&&registers[0x46]==6&&registers[0x45]==14);
 auto actual=b.read_hardware_gains();assert(actual.microphone==18&&actual.reference==37.5&&reads==4);
 fail_channel=1;assert(!b.set_input_gain(21));actual=b.read_hardware_gains();
 assert(std::isnan(actual.microphone)&&actual.reference==37.5&&registers[0x45]==14);
 fail_channel=-1;assert(b.set_input_gain(21));fail_read=0x44;actual=b.read_hardware_gains();assert(std::isnan(actual.microphone));
 fail_read=0x45;actual=b.read_hardware_gains();assert(actual.microphone==21&&std::isnan(actual.reference));
 fail_read=-1;actual=b.read_hardware_gains();assert(actual.microphone==21);
 // Failed write with all channels unchanged is recoverable by readback.
 registers[0x43]=registers[0x44]=registers[0x46]=6;actual=b.read_hardware_gains();
 assert(actual.microphone==18&&b.es7210_.input_gain_db==18);
 assert(!b.set_input_channel_gain(1,24));assert(b.set_input_channel_gain(2,20.5));
 assert(registers[0x45]==7&&registers[0x43]==6);
 CodecDevBackend spot;auto cfg=spot.input_codec_;cfg.input_gain_db=24;spot.set_input_codec_config(cfg);
 assert(spot.normalize_input_gain(23)==18&&spot.normalize_input_gain(37.5)==36);
 assert(std::isnan(spot.normalize_input_gain(42))&&std::isnan(spot.normalize_input_gain(NAN)));
 assert(spot.set_input_gain(37.5)&&spot.input_codec_.input_gain_db==36);
 spot.open_=true;assert(spot.set_input_gain(23));actual=spot.read_hardware_gains();
 assert(actual.microphone==18&&std::isnan(actual.reference));
 spot.open_=false;const auto previous_reads=reads;actual=spot.read_hardware_gains();
 assert(std::isnan(actual.microphone)&&reads==previous_reads);
}
''')


def test_normalization_matches_resolved_vendor(tmp_path):
    root = os.environ.get("ESP_CODEC_DEV_SOURCE")
    if not root:
        pytest.skip("set ESP_CODEC_DEV_SOURCE to verify the resolved vendor driver")
    vendor = (Path(root) / "device/es7210/es7210.c").read_text()
    vendor_method = method(vendor, "static es7210_gain_value_t get_db(")
    prefix = "enum es7210_gain_value_t { GAIN_0DB, GAIN_3DB, GAIN_6DB, GAIN_9DB, GAIN_12DB, GAIN_15DB, GAIN_18DB, GAIN_21DB, GAIN_24DB, GAIN_27DB, GAIN_30DB, GAIN_33DB, GAIN_34_5DB, GAIN_36DB, GAIN_37_5DB };\n"
    # C++ enum addition differs from C, preserve the expression with a cast.
    vendor_method = vendor_method.replace("return GAIN_0DB + idx;", "return static_cast<es7210_gain_value_t>(GAIN_0DB + idx);")
    vendor8311 = method((Path(root) / "device/es8311/es8311.c").read_text(), "static int es8311_adc_set_vol(")
    prefix8311 = r'''
#include <cstddef>
#define ESP_CODEC_DEV_INVALID_ARG -1
#define ESP_CODEC_DEV_WRONG_STATE -2
#define ESP_CODEC_DEV_WRITE_FAIL -3
#define ES8311_ADC_REG16 0x16
enum es8311_mic_gain_t { ES8311_MIC_GAIN_0DB, ES8311_MIC_GAIN_6DB, ES8311_MIC_GAIN_12DB, ES8311_MIC_GAIN_18DB, ES8311_MIC_GAIN_24DB, ES8311_MIC_GAIN_30DB, ES8311_MIC_GAIN_36DB, ES8311_MIC_GAIN_42DB };
struct audio_codec_if_t {};
struct audio_codec_es8311_t { bool is_open=true; };
int actual_scale=0;
int es8311_write_reg(audio_codec_es8311_t*,int,int value){actual_scale=value;return 0;}
'''
    compile_run(tmp_path, backend_code() + prefix + vendor_method + prefix8311 + vendor8311 + r'''
int main(){esphome::esp_audio_stack::CodecDevBackend b;b.es7210_.enabled=true;
 const float levels[]={0,3,6,9,12,15,18,21,24,27,30,33,34.5,36,37.5};
 for(int i=0;i<=3750;i++){float request=i/100.f;assert(b.normalize_input_gain(request)==levels[get_db(request)]);}
 b.es7210_.enabled=false;audio_codec_es8311_t codec;
 for(int i=0;i<=3750;i++){float request=i/100.f;
  assert(es8311_adc_set_vol(reinterpret_cast<audio_codec_if_t*>(&codec),15,request)==0);
  assert(b.normalize_input_gain(request)==actual_scale*6.f);
 }
}
''')
