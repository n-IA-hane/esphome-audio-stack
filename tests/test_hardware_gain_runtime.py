"""Owner-side analog writes, schema constraints and result ordering."""

from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "esphome/components/esp_audio_stack"


def test_gain_requests_run_only_on_the_codec_owner(tmp_path):
    source = (COMPONENT / "audio_codec_controls.cpp").read_text()
    source = source[
        source.index("namespace esphome::esp_audio_stack {") : source.rindex("#endif")
    ]
    cpp = tmp_path / "gain.cpp"
    cpp.write_text(
        r"""
#include <atomic>
#include <cmath>
#include <cassert>
#include <cstdint>
#include <functional>
#define ESP_LOGW(...) ((void)0)
unsigned notifications=0;
void xTaskNotifyGive(void*){++notifications;}
namespace esphome::esp_audio_stack {
struct Backend {
 bool ok=true;unsigned calls=0;float gains[2]{};std::function<void()> during_write;
 bool write(unsigned index,float db){++calls;if(during_write){auto fn=during_write;during_write={};fn();}if(!ok)return false;gains[index]=db;return true;}
 bool set_input_gain(float db){return write(0,db);}
 bool set_input_channel_gain(unsigned ch,float db){assert(ch==2);return write(1,db);}
};
struct ESPAudioStack {
 Backend codec_backend_;void *audio_task_handle_=nullptr;
 bool hardware_reference_gain_available_=false;uint8_t hardware_reference_gain_channel_=0;
 std::atomic<float> hardware_gain_requested_[2]{},hardware_gain_applied_[2]{};
 std::atomic<uint32_t> hardware_gain_revision_[2]{},hardware_gain_result_revision_[2]{};
 std::atomic<bool> hardware_gain_failed_[2]{},hardware_gain_result_pending_{false};
 std::atomic<uint8_t> hardware_gains_dirty_{0};unsigned loop_wakes=0;
 void enable_loop_soon_any_context(){++loop_wakes;}
 void configure_hardware_gain_defaults_(float,bool,uint8_t,float);
 bool request_hardware_gain(bool,float);bool hardware_gain_result(bool,float*)const;
 void apply_pending_hardware_gains_();
};
}
"""
        + source
        + r"""
int main(){using namespace esphome::esp_audio_stack;ESPAudioStack s;
 s.configure_hardware_gain_defaults_(12,true,2,24);
 assert(!s.request_hardware_gain(false,NAN)&&!s.request_hardware_gain(false,38));
 assert(s.request_hardware_gain(false,15));assert(!notifications&&!s.codec_backend_.calls);
 s.apply_pending_hardware_gains_();float result=0;
 assert(s.hardware_gain_result(false,&result)&&result==15);assert(s.codec_backend_.calls==1);
 s.audio_task_handle_=&s;assert(s.request_hardware_gain(true,27));assert(notifications==1);
 s.apply_pending_hardware_gains_();assert(s.hardware_gain_result(true,&result)&&result==27);
 s.codec_backend_.ok=false;assert(s.request_hardware_gain(false,21));s.apply_pending_hardware_gains_();
 assert(s.hardware_gain_result(false,&result)&&result==15);assert(s.hardware_gain_failed_[0]);
 // A newer main-loop request during an older hardware write must not be rolled back.
 s.codec_backend_.ok=true;s.request_hardware_gain(false,18);
 s.codec_backend_.during_write=[&]{s.request_hardware_gain(false,30);};s.apply_pending_hardware_gains_();
 assert(!s.hardware_gain_result(false,&result));s.apply_pending_hardware_gains_();
 assert(s.hardware_gain_result(false,&result)&&result==30);
 ESPAudioStack no_ref;no_ref.configure_hardware_gain_defaults_(0,false,0,0);
 assert(!no_ref.request_hardware_gain(true,10));
}
"""
    )
    subprocess.run(
        [
            "g++",
            "-std=c++17",
            "-Wall",
            "-Wextra",
            "-Werror",
            str(cpp),
            "-o",
            str(tmp_path / "gain"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run([str(tmp_path / "gain")], check=True, capture_output=True, text=True)
