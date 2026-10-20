"""Exercise real microphone/speaker request and loop methods before setup."""

from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_early_capture_and_shared_listener_lifecycle(tmp_path):
    root = ROOT / "esphome/components/esp_audio_stack"
    methods = ""
    for component, cls, names in [
        ("microphone", "ESPAudioStackMicrophone", ["start", "release_listener_", "stop", "loop"]),
        ("speaker", "ESPAudioStackSpeaker", ["start", "stop", "loop"]),
    ]:
        source = (root / component / f"esp_audio_stack_{component}.cpp").read_text()
        for name in names:
            signature = ("bool" if name == "release_listener_" else "void") + " " + cls + "::" + name + "("
            start = source.index(signature)
            methods += source[start:source.index("\n}\n", start) + 2] + "\n"
    harness = r'''
#include <atomic>
#include <cassert>
#include <cstdint>
#include <optional>
#include <thread>
#include <vector>
template<class... T>void log(T...){}
#define ESP_LOGI(...) log(__VA_ARGS__)
#define ESP_LOGW(...) log(__VA_ARGS__)
#define ESP_LOGE(...) log(__VA_ARGS__)
#define LOG_STR(x) x
constexpr const char *TAG="audio";
constexpr uint32_t MAX_LISTENERS=16;
uint32_t millis(){return 100;}
namespace microphone {enum {STATE_STOPPED,STATE_STARTING,STATE_RUNNING,STATE_STOPPING};}
namespace speaker {enum {STATE_STOPPED,STATE_STARTING,STATE_RUNNING,STATE_STOPPING};}
struct Parent {
 bool ready=false;unsigned starts=0,stops=0;
 bool has_i2s_error(){return false;}
 bool register_mic_consumer(void*){assert(ready);++starts;return true;}
 void unregister_mic_consumer(void*){++stops;}
 bool is_running(){return ready;} bool is_starting(){return false;}
 void start_speaker(){assert(ready);++starts;}
 void stop_speaker(){++stops;}
};
struct Base {
 Parent backing;Parent *parent_=&backing;
 unsigned state_=0;bool i2s_error_latched_=false;
 std::atomic<bool> loop_enabled{false};
 bool is_failed(){return false;} bool status_has_error(){return false;}
 void status_set_error(const char*){}void status_clear_error(){}
 void enable_loop_soon_any_context(){loop_enabled=true;}void disable_loop(){loop_enabled=false;}
};
struct ESPAudioStackMicrophone:Base {
 std::atomic<uint32_t> active_listeners_{0};bool parent_registered_=false;
 void start();bool release_listener_();void stop();void loop();
};
struct ESPAudioStackSpeaker:Base {
 std::atomic<bool> listener_registered_{false};bool parent_requested_=false;
 bool finishing_=false,pause_state_=false,buffered=false;
 uint32_t last_write_ms_=0;std::optional<uint32_t> timeout_{500};
 bool has_buffered_data(){return buffered;}
 void set_pause_state(bool value){pause_state_=value;}
 void start();void stop();void loop();
};
'''
    checks = r'''
int main(){
 ESPAudioStackMicrophone mic;
 // Requests are valid before hardware/setup, without touching an RTOS object.
 mic.start();mic.start();assert(mic.active_listeners_==2 && mic.backing.starts==0);
 mic.backing.ready=true;mic.loop();assert(mic.backing.starts==1 && mic.state_==microphone::STATE_RUNNING);
 mic.stop();mic.loop();assert(mic.backing.stops==0 && mic.active_listeners_==1);
 mic.stop();mic.loop();assert(mic.backing.stops==1 && mic.state_==microphone::STATE_STOPPED);
 mic.start();mic.stop();mic.loop();assert(mic.backing.starts==1);
 mic.stop();assert(mic.active_listeners_==0);
 for(unsigned i=0;i<MAX_LISTENERS+2;++i)mic.start();
 assert(mic.active_listeners_==MAX_LISTENERS);
 for(unsigned i=0;i<MAX_LISTENERS+2;++i)mic.stop();assert(mic.active_listeners_==0);
 std::vector<std::thread> threads;
 for(unsigned i=0;i<8;++i)threads.emplace_back([&]{for(unsigned j=0;j<10000;++j){mic.start();mic.stop();}});
 for(auto &thread:threads)thread.join();assert(mic.active_listeners_==0);
 ESPAudioStackSpeaker spk;
 spk.start();spk.start();assert(spk.listener_registered_ && spk.backing.starts==0);
 spk.backing.ready=true;spk.loop();assert(spk.backing.starts==1);
 spk.stop();spk.stop();spk.loop();assert(spk.backing.stops==1 && !spk.listener_registered_);
 spk.start();spk.stop();spk.loop();assert(spk.backing.starts==1);
 spk.start();spk.loop();spk.finishing_=true;spk.buffered=true;spk.loop();assert(spk.listener_registered_);
 spk.buffered=false;spk.loop();spk.loop();assert(!spk.listener_registered_ && spk.backing.stops==2);
}
'''
    cpp = tmp_path / "listeners.cpp"
    cpp.write_text(harness + methods + checks)
    exe = tmp_path / "listeners"
    subprocess.run(["g++", "-std=c++17", "-pthread", "-Wall", "-Wextra",
                    str(cpp), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
