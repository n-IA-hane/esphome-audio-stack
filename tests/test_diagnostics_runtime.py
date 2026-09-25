"""Exercise the production diagnostic collector and logging outside its lock."""

from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "esphome/components/esp_audio_stack"


def test_runtime_snapshot_is_bounded_and_logs_after_unlock(tmp_path):
    source = (COMPONENT / "audio_diagnostics.cpp").read_text()
    source = "\n".join(
        line for line in source.splitlines() if not line.startswith("#include")
    )
    header = (COMPONENT / "esp_audio_stack.h").read_text()
    start = header.index("  struct DiagnosticLayout")
    end = header.index("  void service_speaker_reset_", start)
    fields = header[start:end]
    prelude = r"""
#include <atomic>
#include <cassert>
#include <cstdarg>
#include <cstdio>
#include <cstdint>
#include <mutex>
#include <memory>
#include <functional>
#include <string>
#include <vector>
#define USE_ESP32
#define USE_ESP_AUDIO_STACK_HARDWARE_CODEC
#define portMUX_INITIALIZER_UNLOCKED {}
using portMUX_TYPE=std::mutex;
thread_local int lock_depth=0;
#define portENTER_CRITICAL(p) do{(p)->lock();++lock_depth;}while(false)
#define portEXIT_CRITICAL(p) do{--lock_depth;(p)->unlock();}while(false)
#define YESNO(x) ((x)?"YES":"NO")
std::vector<std::string> lines;
void log(const char*,const char*fmt,...){assert(lock_depth==0);char b[384];va_list a;va_start(a,fmt);vsnprintf(b,sizeof(b),fmt,a);va_end(a);lines.emplace_back(b);}
#define ESP_LOGI(tag,...) log(tag,__VA_ARGS__)
#define ESP_LOGW ESP_LOGI
uint32_t now=2000;uint32_t millis(){return now;}
unsigned uxTaskGetStackHighWaterMark(void*){return 1000;}
namespace esphome::esp_audio_stack {
enum class AudioStackRuntimeState:uint8_t{IDLE};
enum class I2SHardwareState:uint8_t{UNPREPARED,PREPARING,READY,RUNNING,STOPPING,ERROR};
struct CodecDevBackend {
 struct StreamLayout{struct{unsigned sample_rate=48000,data_bit=16,slot_bit=16,total_slot=2,slot_mask=3;}bus;struct{unsigned value=0x21;}memory;bool valid=false;};
 bool prepared=false,open=false;StreamLayout rx,tx;
 bool is_prepared()const{return prepared;}bool is_open()const{return open;}
 const StreamLayout&rx_layout()const{return rx;}const StreamLayout&tx_layout()const{return tx;}
 const char*input_codec_name()const{return "ES8311";}const char*output_codec_name()const{return "ES8311";}
};
struct ESPAudioStack{
 bool failed=false;bool is_failed()const{return failed;}
 std::atomic<uint8_t> i2s_hardware_state_{0};
 std::atomic<bool> has_mic_consumers_{false},speaker_running_{false},speaker_paused_{false},has_i2s_error_{false};
 std::atomic<unsigned> tx_completion_idle_event_drops_{0},tx_completion_pending_real_records_{0};
 unsigned tx_completion_dma_frames_=480,tx_completion_queue_size_=12;
 unsigned i2s_num_=0,sample_rate_=48000,output_sample_rate_=16000,bits_per_sample_=16,slot_bit_width_=0;
 bool use_tdm_bus_=false,use_tdm_ref_=false,use_stereo_aec_ref_=true,mic_channel_right_=false,rx_slot_mode_stereo_=true,ref_channel_right_=true;
 unsigned tdm_mic_slot_=0,tdm_ref_slot_=1,tdm_tx_slot_=0,tdm_total_slots_=4;int tdm_second_mic_slot_=-1;
 int task_core_=0;unsigned task_priority_=19,task_stack_size_=8192;
 bool buffers_in_psram_=true,audio_task_stack_in_psram_=true;void*audio_task_handle_=nullptr;
 CodecDevBackend codec_backend_;
 int compute_runtime_state_()const{return 0;}static const char*runtime_state_to_string(AudioStackRuntimeState){return "idle";}
 static const char*i2s_hardware_state_to_string(I2SHardwareState){return "test";}
 unsigned get_speaker_buffer_size()const{return 48000;}unsigned get_speaker_buffer_available()const{return 1600;}
 std::function<void()> pending;
 void defer(const char*,std::function<void()> fn){assert(!pending);pending=std::move(fn);}
 void finish_dump(){while(pending){auto fn=std::move(pending);pending={};fn();}}
 void dump_diagnostics();
"""
    tail = r"""
int main(){
 using namespace esphome::esp_audio_stack;
 ESPAudioStack s;static_assert(sizeof(s.diagnostic_hardware_)<=64);
 s.publish_diagnostic_hardware_();s.dump_diagnostics();s.finish_dump();
 auto has=[](const char*t){for(auto &s:lines)if(s.find(t)!=std::string::npos)return true;return false;};
 assert(has("RX effective layout: not available"));assert(has("speaker_queued_bytes=1600"));
 const size_t n=lines.size();s.dump_diagnostics();s.finish_dump();assert(lines.size()==n);
 now+=1000;s.codec_backend_.prepared=s.codec_backend_.open=true;
 s.codec_backend_.rx.valid=s.codec_backend_.tx.valid=true;
 s.i2s_hardware_state_=static_cast<uint8_t>(I2SHardwareState::RUNNING);s.publish_diagnostic_hardware_();
 lines.clear();s.dump_diagnostics();s.finish_dump();assert(has("RX effective: rate=48000"));assert(has("prepared=YES open=YES"));
 assert(!has("memory:"));assert(!has("total_slots=4"));
 now+=1000;s.i2s_hardware_state_=static_cast<uint8_t>(I2SHardwareState::STOPPING);
 lines.clear();s.dump_diagnostics();s.finish_dump();assert(has("transition=YES"));assert(has("RX effective layout: not available"));
 s.codec_backend_.prepared=s.codec_backend_.open=false;s.i2s_hardware_state_=0;s.publish_diagnostic_hardware_();
 now+=1000;lines.clear();s.dump_diagnostics();s.finish_dump();assert(has("prepared=NO open=NO"));
 assert(lock_depth==0);
}
"""
    cpp = tmp_path / "diagnostics.cpp"
    cpp.write_text(prelude + fields + "};\n}\n" + source + tail)
    exe = tmp_path / "diagnostics"
    subprocess.run(
        [
            "g++",
            "-std=c++17",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-fsanitize=address,undefined",
            str(cpp),
            "-o",
            str(exe),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run([str(exe)], check=True, capture_output=True, text=True)
