"""The public start request must not execute slow codec initialization."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_start_does_not_open_hardware_on_the_calling_thread(tmp_path):
    source = (ROOT / 'esphome/components/esp_audio_stack/esp_audio_stack.cpp').read_text()
    start = source.index('void ESPAudioStack::start() {')
    method = source[start:source.index('\n}', start) + 2]
    harness = r'''
#include <atomic>
#include <cassert>
#include <cstdint>
#define ESP_LOGI(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
using BaseType_t=int;
constexpr const char *TAG="audio";
constexpr int tskNO_AFFINITY=-1, pdTRUE=1;
int wakeups=0;
void xTaskNotifyGive(void*){++wakeups;}
template<class... T> bool start_pinned_task(T...){return true;}
struct Callback {void call(){}};
struct ESPAudioStack {
 std::atomic<bool> audio_stack_running_{false}, teardown_pending_{false},has_i2s_error_{false};
 void *audio_task_handle_=this,*audio_task_tcb_=nullptr,*audio_task_stack_=nullptr;
 int task_core_=1,task_stack_size_=4096,task_priority_=5;bool audio_task_stack_in_psram_=false;
 std::atomic<bool> start_requested_{false};
 std::atomic<uint32_t> start_generation_{0};
 bool is_running()const{return audio_stack_running_;}
 Callback start_callback_;int hardware_opens=0;
 static void audio_task(void*){}
 bool wait_audio_task_idle_(unsigned){return true;}
 void deinit_i2s_(){}void request_audio_preallocation_(){}
 bool enable_i2s_channels_(){++hardware_opens;return true;}
 void start();
};
'''
    checks = r'''
int main(){
 ESPAudioStack stack;stack.start();
 assert(stack.hardware_opens==0 && "start must only request work from the existing audio task");
 assert(!stack.audio_stack_running_ && "hardware is not ready merely because start was requested");
 assert(wakeups>0);
}
'''
    cpp = tmp_path / 'async_start.cpp'
    cpp.write_text(harness + method + checks)
    exe = tmp_path / 'async_start'
    subprocess.run(['g++', '-std=c++17', str(cpp), '-o', str(exe)], check=True, capture_output=True, text=True)
    result = subprocess.run([str(exe)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_hardware_completion_cancellation_and_retry_have_one_owner(tmp_path):
    source = (ROOT / 'esphome/components/esp_audio_stack/esp_audio_stack.cpp').read_text()
    methods = []
    for name in ('start', 'stop', 'service_audio_lifecycle_', 'finish_audio_lifecycle_'):
        start = source.index(f'void ESPAudioStack::{name}() {{')
        methods.append(source[start:source.index('\n}', start) + 2])
    header = (ROOT / 'esphome/components/esp_audio_stack/esp_audio_stack.h').read_text()
    start = header.index('enum class AudioStartPhase')
    phase = header[start:header.index(';', start) + 1]
    harness = r'''
#include <atomic>
#include <cassert>
#include <condition_variable>
#include <cstdint>
#include <functional>
#include <mutex>
#include <thread>
#define ESP_LOGI(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
using BaseType_t=int;
constexpr const char *TAG="audio";
constexpr int tskNO_AFFINITY=-1;
std::atomic<unsigned> wakeups{0};
void xTaskNotifyGive(void*){++wakeups;}
template<class... T> bool start_pinned_task(T...){return true;}
struct Callback {unsigned count=0;std::function<void()> fn;void call(){++count;if(fn)fn();}};
struct Processor {std::function<void(bool)> fn;void set_processing_active(bool active){if(fn)fn(active);}};
struct ObservedGeneration {
 std::atomic<uint32_t> value{0};mutable std::function<void()> before_load;
 ObservedGeneration(uint32_t v):value(v){}
 uint32_t load(std::memory_order order)const{if(before_load){auto fn=before_load;before_load={};fn();}return value.load(order);}
 void store(uint32_t v,std::memory_order order){value.store(v,order);}
};
struct ESPAudioStack {
 std::atomic<bool> audio_stack_running_{false}, teardown_pending_{false},has_i2s_error_{false};
 std::atomic<bool> start_requested_{false},speaker_requested_{false},speaker_running_{false},has_mic_consumers_{false};
 Processor *processor_=nullptr;
 std::atomic<bool> audio_task_idle_{true},hardware_volume_dirty_{false},tx_completion_tracking_active_{false};
 std::atomic<AudioStartPhase> start_phase_{AudioStartPhase::STOPPED};
 std::atomic<uint32_t> start_generation_{0},lifecycle_events_{0};
 ObservedGeneration ready_generation_{0};
 uint32_t handled_lifecycle_events_=0;
 void *audio_task_handle_=this,*audio_task_tcb_=nullptr,*audio_task_stack_=nullptr;
 int task_core_=1,task_stack_size_=4096,task_priority_=5;bool audio_task_stack_in_psram_=false;
 Callback start_callback_,idle_callback_,speaker_idle_callback_,lifecycle_callback_;
 std::atomic<unsigned> opens{0},closes{0};bool fail_open=false;std::atomic<bool> hardware_open{false};
 std::function<void()> during_open;std::thread::id worker;
 static void audio_task(void*){}
 bool is_running()const{return audio_stack_running_;}
 bool is_starting()const{return start_requested_ && !is_running();}
 void enable_loop_soon_any_context(){}void request_audio_preallocation_(){}
 void update_runtime_state_(){}void apply_pending_hardware_volume_(){}
 void start_speaker(){speaker_running_=true;}
 bool enable_i2s_channels_(){worker=std::this_thread::get_id();++opens;if(during_open)during_open();hardware_open=!fail_open;return !fail_open;}
 void deinit_i2s_(){assert(std::this_thread::get_id()==worker);++closes;hardware_open=false;}
 void start();void stop();void service_audio_lifecycle_();void finish_audio_lifecycle_();
};
'''
    checks = r'''
void prepare(ESPAudioStack &s){s.start();s.service_audio_lifecycle_();assert(!s.is_running());s.finish_audio_lifecycle_();assert(s.is_running());}
int main(){
 const auto main_thread=std::this_thread::get_id();
 {
  ESPAudioStack s;s.start();
  s.idle_callback_.fn=[&]{assert(s.teardown_pending_);};
  s.stop(); // Even cancellation before open publishes pending cleanup first.
 }
 {
  ESPAudioStack s;prepare(s);
  s.idle_callback_.fn=[&]{assert(s.teardown_pending_);s.start();};
  s.stop();assert(s.is_starting());s.service_audio_lifecycle_();s.finish_audio_lifecycle_();
  assert(s.is_running() && s.closes==1 && s.opens==2 && s.start_callback_.count==2);
 }
 {
  // The other core supersedes the READY snapshot while main reads its generation.
  ESPAudioStack s;s.start_requested_=true;s.start_generation_=2;s.lifecycle_events_=1;
  s.ready_generation_.store(2,std::memory_order_release);s.start_phase_=AudioStartPhase::READY;
  s.ready_generation_.before_load=[&]{s.start_phase_=AudioStartPhase::STARTING;};
  s.finish_audio_lifecycle_();assert(!s.is_running() && s.start_callback_.count==0);
  s.start_phase_=AudioStartPhase::READY;s.lifecycle_events_=2;s.finish_audio_lifecycle_();
  assert(s.is_running() && s.start_callback_.count==1);
 }
 {
  ESPAudioStack s;Processor p;s.processor_=&p;s.has_mic_consumers_=true;
  s.start();s.service_audio_lifecycle_();const auto before=wakeups.load();bool enabled=false;
  p.fn=[&](bool active){if(active){assert(wakeups>before && s.start_phase_==AudioStartPhase::RUNNING);enabled=true;}};
  s.finish_audio_lifecycle_();assert(enabled && s.is_running());
 }
 {
  ESPAudioStack s;s.start();s.start();assert(s.opens==0);s.service_audio_lifecycle_();
  assert(s.opens==1 && s.hardware_open && !s.is_running() && s.start_callback_.count==0);
  s.start_callback_.fn=[&]{assert(std::this_thread::get_id()==main_thread);};
  s.finish_audio_lifecycle_();s.finish_audio_lifecycle_();assert(s.is_running() && s.start_callback_.count==1);
  s.stop();s.stop();assert(s.closes==0 && !s.is_running() && s.idle_callback_.count==1);
  s.service_audio_lifecycle_();s.finish_audio_lifecycle_();assert(!s.hardware_open && s.closes==1);
  prepare(s);assert(s.opens==2 && s.start_callback_.count==2);
 }
 {
  ESPAudioStack s;std::mutex mutex;std::condition_variable cv;bool entered=false,released=false;
  s.during_open=[&]{std::unique_lock lock(mutex);entered=true;cv.notify_one();cv.wait(lock,[&]{return released;});};
  s.start();std::thread task([&]{s.service_audio_lifecycle_();});
  {std::unique_lock lock(mutex);cv.wait(lock,[&]{return entered;});}
  s.stop();assert(!s.is_running() && s.start_callback_.count==0);
  {std::lock_guard lock(mutex);released=true;}cv.notify_one();task.join();
  s.finish_audio_lifecycle_();assert(!s.hardware_open && s.closes==1 && s.start_callback_.count==0);
 }
 {
  ESPAudioStack s;s.during_open=[&]{s.stop();s.start();};s.start();s.service_audio_lifecycle_();
  s.finish_audio_lifecycle_();assert(s.start_callback_.count==0 && !s.hardware_open);
  s.during_open={};s.service_audio_lifecycle_();s.finish_audio_lifecycle_();
  assert(s.opens==2 && s.closes==1 && s.is_running() && s.start_callback_.count==1);
 }
 {
  ESPAudioStack s;s.fail_open=true;s.start();s.service_audio_lifecycle_();s.finish_audio_lifecycle_();
  assert(!s.hardware_open && !s.is_starting() && !s.is_running() && s.has_i2s_error_);
  for(unsigned i=0;i<10;++i)s.service_audio_lifecycle_();assert(s.opens==1);
  s.fail_open=false;prepare(s);assert(s.opens==2 && !s.has_i2s_error_);
 }
 {
  ESPAudioStack s;s.start();s.service_audio_lifecycle_();s.stop();s.finish_audio_lifecycle_();
  assert(s.start_callback_.count==0);s.service_audio_lifecycle_();assert(!s.hardware_open);
 }
 {
  ESPAudioStack s;s.start_callback_.fn=[&]{s.stop();};s.start();s.service_audio_lifecycle_();s.finish_audio_lifecycle_();
  assert(!s.is_running() && !s.start_requested_); // cancellation from on_start follows a real READY commit
  s.service_audio_lifecycle_();assert(!s.hardware_open && s.closes==1);
 }
}
'''
    cpp = tmp_path / 'lifecycle.cpp'
    cpp.write_text('#include <cstdint>\n' + phase + harness + '\n'.join(methods) + checks)
    exe = tmp_path / 'lifecycle'
    compiled = subprocess.run(['g++', '-std=c++17', '-pthread', '-Wall', '-Wextra',
                              '-Werror', '-Wno-misleading-indentation', '-DUSE_AUDIO_PROCESSOR',
                              str(cpp), '-o', str(exe)], capture_output=True, text=True)
    assert compiled.returncode == 0, compiled.stderr
    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
