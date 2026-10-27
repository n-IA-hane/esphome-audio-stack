#pragma once
#include <algorithm>
#include <atomic>
#include <cassert>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <deque>
#include <functional>
#include <memory>
#include <map>
#include <mutex>
#include <thread>
#include <vector>
#include "observer.h"
#define USE_ESP32
#define USE_ESP_AUDIO_STACK_CAPTURE
#define ESP_LOGCONFIG(...) ((void)0)
namespace esphome {
class Component {
 public:
 virtual ~Component()=default;virtual void setup(){}virtual void loop(){}virtual void dump_config(){}virtual void on_shutdown(){}
 void mark_failed(){failed=true;}bool is_failed(){return failed;}void disable_loop(){}void enable_loop(){}void enable_loop_soon_any_context(){}
 void status_momentary_warning(const char*,unsigned){}bool failed=false;
};
template<class... Ts>class Action {public:virtual void play(const Ts&...)=0;};
}
struct HostTask {
 std::thread thread;std::mutex mutex;std::condition_variable cv;unsigned notifications=0;bool deleted=false;
};
using TaskHandle_t=HostTask*;using SemaphoreHandle_t=std::mutex*;
constexpr int pdPASS=1,pdTRUE=1;constexpr unsigned portMAX_DELAY=UINT32_MAX;
inline unsigned pdMS_TO_TICKS(unsigned n){return n;}
inline thread_local HostTask *current_task=nullptr;
inline std::atomic<unsigned> task_count=0;
inline int xTaskCreate(void(*fn)(void*),const char*,unsigned,void *arg,int,TaskHandle_t *handle){
 auto *task=new HostTask;*handle=task;++task_count;task->thread=std::thread([=]{current_task=task;fn(arg);});return pdPASS;
}
inline void xTaskNotifyGive(TaskHandle_t task){std::lock_guard guard(task->mutex);++task->notifications;task->cv.notify_all();}
inline unsigned ulTaskNotifyTake(int,unsigned timeout){
 auto &task=*current_task;std::unique_lock lock(task.mutex);
 if(timeout==portMAX_DELAY)task.cv.wait(lock,[&]{return task.notifications||task.deleted;});
 else task.cv.wait_for(lock,std::chrono::milliseconds(timeout),[&]{return task.notifications||task.deleted;});
 unsigned count=task.notifications;task.notifications=0;return count;
}
inline void vTaskSuspend(void*){auto &task=*current_task;std::unique_lock lock(task.mutex);task.cv.wait(lock,[&]{return task.deleted;});}
inline void vTaskDelete(TaskHandle_t task){{std::lock_guard guard(task->mutex);task->deleted=true;task->cv.notify_all();}task->thread.join();delete task;--task_count;}
inline SemaphoreHandle_t xSemaphoreCreateMutex(){static std::mutex lock;return &lock;}
inline void xSemaphoreTake(SemaphoreHandle_t lock,unsigned){lock->lock();}
inline void xSemaphoreGive(SemaphoreHandle_t lock){lock->unlock();}
inline std::atomic<int64_t> capture_clock_override_us{-1};
inline int64_t esp_timer_get_time(){const auto now=capture_clock_override_us.load();return now>=0?now:std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();}
namespace esphome::esp_audio_stack {
inline std::atomic<unsigned> queues=0;
class HostRing {
 std::mutex mutex_;std::deque<std::pair<void*,size_t>> queue_;size_t used_=0;std::map<void*,size_t> borrowed_;
 public:
 size_t capacity,peak=0;
 std::atomic<bool> pause_next=false;std::mutex gate_mutex;std::condition_variable gate_cv;bool entered=false,released=false;
 explicit HostRing(size_t size):capacity(size){++queues;}
 ~HostRing(){reset();assert(borrowed_.empty());--queues;}
 void *acquire_item(size_t size){std::unique_lock guard(mutex_);if(size>capacity/2||used_+size>capacity)return nullptr;auto*p=malloc(size);borrowed_[p]=size;used_+=size;peak=std::max(peak,used_);guard.unlock();
  if(pause_next.exchange(false)){std::unique_lock lock(gate_mutex);entered=true;gate_cv.notify_all();gate_cv.wait(lock,[&]{return released;});}return p;}
 void complete_item(void *p){std::lock_guard guard(mutex_);queue_.emplace_back(p,borrowed_.at(p));borrowed_.erase(p);}
 void *receive_item(size_t *size){std::lock_guard guard(mutex_);if(queue_.empty())return nullptr;auto item=queue_.front();queue_.pop_front();*size=item.second;borrowed_[item.first]=item.second;return item.first;}
 void return_item(void*p){std::lock_guard guard(mutex_);used_-=borrowed_.at(p);borrowed_.erase(p);free(p);}
 void reset(){std::lock_guard guard(mutex_);for(auto [p,size]:queue_){used_-=size;free(p);}queue_.clear();}
};
using RingBufferPtr=std::unique_ptr<HostRing>;
inline RingBufferPtr create_nosplit_prefer_psram(size_t bytes,const char*){return std::make_unique<HostRing>(bytes);}
struct ESPAudioStack {AudioCaptureObserver *observer=nullptr;uint32_t generation=10;bool failed=false;
 unsigned consumers=1;void *capture_token=nullptr;bool admit=true;
 bool register_mic_consumer(void *token){if(!admit)return false;assert(capture_token==nullptr);capture_token=token;++consumers;return true;}
 void unregister_mic_consumer(void *token){assert(capture_token==token);capture_token=nullptr;--consumers;}
 void set_capture_observer(AudioCaptureObserver *p){observer=p;}uint32_t capture_generation(){return generation;}bool has_failed_session(){return failed;}};
struct ESPAudioStackMicrophone {std::atomic<bool> muted=false;unsigned listeners=1,starts=0,stops=0;
 bool get_mute_state(){return muted;}void start(){if(listeners<16){++listeners;++starts;}}void stop(){assert(listeners);--listeners;++stops;}};
}
