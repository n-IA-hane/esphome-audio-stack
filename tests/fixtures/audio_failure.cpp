// Host hardware/RTOS adapters. Lifecycle, failure and child methods are read
// from the production component by test_audio_failure_runtime.py.
#include <algorithm>
#include <atomic>
#include <cassert>
#include <cstdint>
#include <cstring>
#include <functional>
#include <optional>
#include <string>
#include <vector>
#define USE_AUDIO_PROCESSOR
#define ESP_LOGI(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
#define ESP_LOGD(...) ((void)0)
#define LOG_STR(x) x
using BaseType_t=int;
using TickType_t=unsigned;
constexpr const char *TAG="test";
constexpr int tskNO_AFFINITY=-1;
constexpr uint32_t MAX_LISTENERS=16;
void xTaskNotifyGive(void*){}
void vTaskDelay(unsigned){}
uint32_t millis(){return 100;}
template<class... T> bool start_pinned_task(T...){return true;}
struct Callback {unsigned count=0;void call(){++count;}};
struct Processor {bool active=false;void set_processing_active(bool v){active=v;}};
struct Ring {
  size_t queued=128,capacity=4096;unsigned resets=0;
  void reset(){queued=0;++resets;}
  size_t free()const{return capacity-queued;}
  size_t write_without_replacement(const void*,size_t n,unsigned,bool){auto k=std::min(n,free());queued+=k;return k;}
};
struct ESPAudioStack {
  std::atomic<bool> audio_stack_running_{false},teardown_pending_{false},has_i2s_error_{false};
  std::atomic<bool> start_requested_{false},speaker_requested_{true},speaker_running_{false},has_mic_consumers_{true};
  std::atomic<bool> audio_task_idle_{true},hardware_volume_dirty_{false},tx_completion_tracking_active_{false};
  std::atomic<bool> request_speaker_reset_{false};
  std::atomic<AudioStartPhase> start_phase_{AudioStartPhase::STOPPED};
  std::atomic<uint32_t> start_generation_{0},ready_generation_{0},lifecycle_events_{0};
  uint32_t handled_lifecycle_events_=0;
  Processor processor;Processor *processor_=&processor;
  Ring ring;Ring *speaker_buffer_=&ring;
  void *audio_task_handle_=this,*audio_task_tcb_=nullptr,*audio_task_stack_=nullptr;
  int task_core_=1,task_stack_size_=4096,task_priority_=5;bool audio_task_stack_in_psram_=false;
  Callback start_callback_,idle_callback_,speaker_idle_callback_,speaker_start_callback_,lifecycle_callback_;
  unsigned opens=0,closes=0;bool fail_open=false,hardware_open=false;
  static void audio_task(void*){}
  bool is_running()const{return audio_stack_running_;}
  bool is_starting()const{return start_requested_&&!is_running();}
  bool has_failed_session()const;
  bool has_i2s_error()const{return has_i2s_error_;}
  void enable_loop_soon_any_context(){}void request_audio_preallocation_(){}
  void update_runtime_state_(){}void apply_pending_hardware_volume_(){}
  bool enable_i2s_channels_(){++opens;hardware_open=!fail_open;return !fail_open;}
  void deinit_i2s_(){++closes;hardware_open=false;}
  bool register_mic_consumer(void*){return true;}
  void unregister_mic_consumer(void*){}
  void reset_tx_playback_(){}
  unsigned get_speaker_channels()const{return 1;}
  unsigned get_speaker_buffer_available()const{return ring.queued;}
  bool has_pending_speaker_output()const{return false;}
  void start();void stop();void service_audio_lifecycle_();void finish_audio_lifecycle_();
  void start_speaker();void stop_speaker();void service_speaker_reset_();
  void fail_tx_completion_tracking_(const char*);void fail_audio_session_(const char*);
  size_t play(const uint8_t*,size_t,TickType_t);
};
namespace speaker {enum State {STATE_STOPPED,STATE_STARTING,STATE_RUNNING,STATE_STOPPING};}
namespace microphone {enum State {STATE_STOPPED,STATE_STARTING,STATE_RUNNING,STATE_STOPPING};}
struct ESPAudioStackSpeaker {
  ESPAudioStack *parent_;
  speaker::State state_=speaker::STATE_RUNNING;
  std::atomic<bool> listener_registered_{true},implicit_recovery_used_{false};
  bool parent_requested_=true,i2s_error_latched_=false,finishing_=true,pause_state_=false,error=false;
  std::optional<unsigned> timeout_{500};unsigned last_write_ms_=0;
  bool is_failed()const{return false;}
  void status_set_error(const char*){error=true;}
  void status_clear_error(){error=false;}
  bool status_has_error()const{return error;}
  void disable_loop(){}void enable_loop_soon_any_context(){}
  void set_pause_state(bool v){pause_state_=v;}
  void start();void stop();void loop();bool has_buffered_data()const;
  size_t play(const uint8_t*,size_t,TickType_t);
};
struct ESPAudioStackMicrophone {
  ESPAudioStack *parent_;
  microphone::State state_=microphone::STATE_RUNNING;
  std::atomic<uint32_t> active_listeners_{1};
  bool parent_registered_=true,i2s_error_latched_=false,error=false;
  bool is_failed()const{return false;}
  void status_set_error(const char*){error=true;}
  void status_clear_error(){error=false;}
  bool status_has_error()const{return error;}
  void disable_loop(){}void enable_loop_soon_any_context(){}
  void start();void stop();void loop();bool release_listener_();
};
