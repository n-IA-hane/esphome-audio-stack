// Host adapters model RTOS ownership, not the ESP-SR algorithm or its timing.
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
#include <future>
#include <memory>
#include <mutex>
#include <thread>
#include <string>
#include <vector>
#define USE_ESP_AFE_GMF_PATH
#define USE_AUDIO_PROCESSOR
#ifndef CONFIG_FREERTOS_TASK_CREATE_ALLOW_EXT_MEM
#define CONFIG_FREERTOS_TASK_CREATE_ALLOW_EXT_MEM 1
#endif
#ifndef configSUPPORT_STATIC_ALLOCATION
#define configSUPPORT_STATIC_ALLOCATION 1
#endif
#define ESP_LOGE(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
#define ESP_LOGI(...) ((void)0)
#define ESP_LOGD(...) ((void)0)
#define ESP_RETURN_ON_FALSE(c,e,...) do {if(!(c)) return(e);} while(0)
#define pdMS_TO_TICKS(x) (x)
constexpr int ESP_GMF_ERR_OK=0,ESP_GMF_ERR_INVALID_ARG=-1,ESP_GMF_ERR_TIMEOUT=-2;
constexpr int ESP_GMF_IO_OK=0,ESP_GMF_IO_FAIL=-1,ESP_FAIL=-1,VAD_SPEECH=1,pdTRUE=1,pdPASS=1;
constexpr bool ESP_AFE_DIAGNOSTIC_COUNTERS=true;
constexpr unsigned CONFIG_MUTEX_TIMEOUT=10,DRAIN_WAIT_TIMEOUT=20,portMAX_DELAY=UINT32_MAX;
constexpr unsigned AFE_RUN_EVENT=1,AFE_DESTROYING_EVENT=2,FEED_TASK_DESTROYED=4,FETCH_TASK_DESTROYED=8;
constexpr unsigned ESP_AFE_FEATURE_VAD=1;
const char *TAG="afe";
using TickType_t=unsigned;using EventBits_t=unsigned;using esp_gmf_err_t=int;using esp_gmf_err_io_t=int;
std::mutex observe_mutex;std::condition_variable observed;
template<class F>void until(F predicate){std::unique_lock lock(observe_mutex);assert(observed.wait_for(lock,std::chrono::seconds(5),predicate));}
struct EventGroup {std::mutex mutex;std::condition_variable changed;unsigned bits=0;};
using EventGroupHandle_t=EventGroup*;
unsigned xEventGroupWaitBits(EventGroup *e,unsigned wanted,bool clear,bool all,unsigned wait){
 std::unique_lock lock(e->mutex);auto ready=[&]{return all?(e->bits&wanted)==wanted:(e->bits&wanted)!=0;};
 if(wait==portMAX_DELAY)e->changed.wait(lock,ready);else e->changed.wait_for(lock,std::chrono::milliseconds(wait),ready);
 auto bits=e->bits;if(clear)e->bits&=~wanted;return bits;
}
std::atomic<unsigned> destroying{0},completed_fetches{0};
void xEventGroupSetBits(EventGroup *e,unsigned bits){{std::lock_guard lock(e->mutex);e->bits|=bits;}e->changed.notify_all();if(bits&AFE_DESTROYING_EVENT)++destroying;if(bits&FETCH_TASK_DESTROYED)++completed_fetches;observed.notify_all();}
void xEventGroupClearBits(EventGroup *e,unsigned bits){std::lock_guard lock(e->mutex);e->bits&=~bits;}
void vEventGroupDelete(EventGroup *e){delete e;}
using SemaphoreHandle_t=std::mutex*;
int xSemaphoreTake(SemaphoreHandle_t m,unsigned){m->lock();return pdTRUE;}
void xSemaphoreGive(SemaphoreHandle_t m){m->unlock();}
void vSemaphoreDelete(SemaphoreHandle_t m){delete m;}
struct TaskExit{};
void vTaskDelete(void*);
void vTaskSuspend(void*){throw TaskExit{};}
struct HostTask {std::mutex mutex;std::condition_variable changed;unsigned notifications=0;std::thread thread;unsigned *owned_stack=nullptr;};
using TaskHandle_t=HostTask*;using StackType_t=unsigned;struct StaticTask_t{};
struct BinarySemaphore {std::mutex mutex;std::condition_variable changed;bool given=false;};
using StaticSemaphore_t=BinarySemaphore;
BinarySemaphore *xSemaphoreCreateBinaryStatic(StaticSemaphore_t *s){return s;}
int xSemaphoreTake(BinarySemaphore *s,unsigned wait){std::unique_lock lock(s->mutex);if(wait==portMAX_DELAY)s->changed.wait(lock,[&]{return s->given;});if(!s->given)return 0;s->given=false;return pdTRUE;}
void xSemaphoreGive(BinarySemaphore *s){{std::lock_guard lock(s->mutex);s->given=true;}s->changed.notify_all();}
std::atomic<unsigned> owners_deleted{0};
void vTaskDelete(void *handle){if(!handle)throw TaskExit{};auto *t=static_cast<HostTask*>(handle);t->thread.join();delete t;++owners_deleted;}
thread_local HostTask *current_task=nullptr;
TaskHandle_t xTaskGetCurrentTaskHandle(){return current_task;}
void xTaskNotifyGive(TaskHandle_t task){if(!task)return;{std::lock_guard lock(task->mutex);++task->notifications;}task->changed.notify_all();observed.notify_all();}
unsigned ulTaskNotifyTake(int clear,unsigned wait){
 if(!current_task)return 0;
 observed.notify_all();
 std::unique_lock lock(current_task->mutex);auto ready=[&]{return current_task->notifications!=0;};
 if(wait==portMAX_DELAY)current_task->changed.wait(lock,ready);else current_task->changed.wait_for(lock,std::chrono::milliseconds(wait),ready);
 auto n=current_task->notifications;if(clear)current_task->notifications=0;else if(n)--current_task->notifications;return n;
}
std::atomic<unsigned> workers{0};bool fail_worker=false;
using UBaseType_t=unsigned;
constexpr unsigned MALLOC_CAP_SPIRAM=1,MALLOC_CAP_INTERNAL=2,MALLOC_CAP_8BIT=4;
bool psram_present=true;unsigned psram_failure=0,external_attempts=0,internal_attempts=0;
unsigned external_allocations=0,external_releases=0;StackType_t *live_external_stack=nullptr;
unsigned caps_deletions=0;
size_t heap_caps_get_total_size(unsigned caps){assert(caps==MALLOC_CAP_SPIRAM);return psram_present?1048576:0;}
void heap_caps_free(StackType_t *stack){
 assert(stack!=nullptr&&stack==live_external_stack&&owners_deleted==1);
 delete[] stack;live_external_stack=nullptr;++external_releases;
}
namespace esp_audio_stack {
struct ScopedLock {SemaphoreHandle_t mutex;ScopedLock(SemaphoreHandle_t m,unsigned):mutex(m){mutex->lock();}~ScopedLock(){mutex->unlock();}operator bool()const{return true;}};
bool start_pinned_task(void(*entry)(void*),const char*,unsigned bytes,void *ctx,int,int,bool external,const char*,TaskHandle_t *out,StaticTask_t*,StackType_t **stack){
 assert(bytes==12288);*out=nullptr;
 if(external){
  ++external_attempts;*stack=nullptr;
  if(fail_worker||psram_failure==1)return false;
  *stack=new StackType_t[bytes/sizeof(StackType_t)];live_external_stack=*stack;++external_allocations;
  // Match the existing helper: failed task creation frees its own allocation
  // before returning false; the caller may then use the internal path.
  if(psram_failure==2){delete[] *stack;*stack=nullptr;live_external_stack=nullptr;++external_releases;return false;}
 }else{++internal_attempts;if(fail_worker)return false;}
 ++workers;*out=new HostTask;auto *task=*out;task->thread=std::thread([=]{current_task=task;try{entry(ctx);}catch(TaskExit&){};});return true;
}
}
// Model the ownership postcondition of the official IDF WithCaps pair. Its
// remote-core synchronization is supplied by host thread join, not timing.
int xTaskCreatePinnedToCoreWithCaps(void(*entry)(void*),const char *name,unsigned bytes,void *ctx,int priority,TaskHandle_t *out,int core,unsigned caps){
 StaticTask_t tcb;StackType_t *stack=nullptr;
 if(!esp_audio_stack::start_pinned_task(entry,name,bytes,ctx,priority,core,(caps&MALLOC_CAP_SPIRAM)!=0,TAG,out,&tcb,&stack))return 0;
 (*out)->owned_stack=stack;return pdPASS;
}
int xTaskCreatePinnedToCore(void(*entry)(void*),const char *name,unsigned bytes,void *ctx,int priority,TaskHandle_t *out,int core){
 StaticTask_t tcb;StackType_t *stack=nullptr;
 return esp_audio_stack::start_pinned_task(entry,name,bytes,ctx,priority,core,false,TAG,out,&tcb,&stack)?pdPASS:0;
}
void vTaskDeleteWithCaps(TaskHandle_t handle){
 auto *stack=handle->owned_stack;vTaskDelete(handle);
 if(stack!=nullptr)heap_caps_free(stack);
 ++caps_deletions;
}
struct Request {char mode[32]{};};
struct HostQueue {std::mutex mutex;std::condition_variable changed;std::deque<Request> entries;};
using QueueHandle_t=HostQueue*;using StaticQueue_t=HostQueue;
QueueHandle_t xQueueCreateStatic(unsigned capacity,unsigned size,uint8_t*,StaticQueue_t *q){assert(capacity==1&&size==sizeof(Request));return q;}
int xQueueOverwrite(QueueHandle_t q,const Request *r){{std::lock_guard lock(q->mutex);q->entries.clear();q->entries.push_back(*r);}q->changed.notify_all();return pdTRUE;}
int xQueueReceive(QueueHandle_t q,Request *r,unsigned wait){std::unique_lock lock(q->mutex);if(wait==portMAX_DELAY)q->changed.wait(lock,[&]{return !q->entries.empty();});if(q->entries.empty())return 0;*r=q->entries.front();q->entries.pop_front();return pdTRUE;}
unsigned uxQueueMessagesWaiting(QueueHandle_t q){std::lock_guard lock(q->mutex);return q->entries.size();}
struct afe_fetch_result_t {int ret_value=0;int16_t *data=nullptr;int data_size=0;int vad_state=VAD_SPEECH;};
using Result=void(*)(afe_fetch_result_t*,void*);using Read=int32_t(*)(void*,int,void*,uint32_t);
using esp_gmf_afe_manager_result_cb_t=Result;using esp_gmf_afe_manager_read_cb_t=Read;
struct Dsp {std::mutex mutex;std::condition_variable changed;std::atomic<bool> entered{false};bool released=false;std::vector<int16_t> pcm;afe_fetch_result_t result;};
struct Iface {afe_fetch_result_t*(*fetch)(Dsp*);void(*destroy)(Dsp*);};
struct ManagerTask {TaskHandle_t task=nullptr;std::atomic<bool> running{true};};
struct esp_gmf_afe_manager_t {
 ManagerTask feed,fetch;Read read_cb=nullptr;void *read_ctx=nullptr;Dsp *afe_data=nullptr;
 Result result_proc=nullptr;void *result_ctx=nullptr;EventGroupHandle_t ctrl_events=nullptr;
 SemaphoreHandle_t read_cb_lock=nullptr,result_cb_lock=nullptr;Iface *esp_afe=nullptr;
 bool vad_enabled=true;
};
using esp_gmf_afe_manager_handle_t=esp_gmf_afe_manager_t*;
int esp_gmf_afe_manager_enable_features(esp_gmf_afe_manager_handle_t m,unsigned feature,bool enabled){
 assert(m!=nullptr&&feature==ESP_AFE_FEATURE_VAD);m->vad_enabled=enabled;return 0;
}
std::atomic<unsigned> managers_created{0},managers_destroyed{0};
std::vector<std::unique_ptr<HostTask>> fetch_threads;
std::atomic<ManagerTask*> current_fetch_task{nullptr};
afe_fetch_result_t *fetch(Dsp *d){
 {std::unique_lock lock(d->mutex);d->entered=true;observed.notify_all();d->changed.wait(lock,[&]{return d->released;});}
 // One controlled fetch completes in this host scenario; the real loop body
 // and destruction acknowledgement still execute unchanged.
 current_fetch_task.load()->running=false;return &d->result;
}
void destroy_dsp(Dsp *d){delete d;}
Iface iface{fetch,destroy_dsp};
void heap_caps_free(esp_gmf_afe_manager_t *manager){++managers_destroyed;delete manager;observed.notify_all();}
esp_gmf_err_t esp_gmf_afe_manager_suspend(esp_gmf_afe_manager_handle_t,bool);
// MANAGER_METHODS
esp_gmf_afe_manager_handle_t create_manager(){
 auto *m=new esp_gmf_afe_manager_t;m->ctrl_events=new EventGroup;m->read_cb_lock=new std::mutex;m->result_cb_lock=new std::mutex;m->afe_data=new Dsp;m->esp_afe=&iface;
 unsigned sequence=++managers_created;m->afe_data->pcm=sequence==1?std::vector<int16_t>{111,222,333,444}:std::vector<int16_t>{555,666,777,888};
 m->afe_data->result={0,m->afe_data->pcm.data(),8,VAD_SPEECH};
 auto thread=std::make_unique<HostTask>();m->fetch.task=thread.get();auto *ctx=thread.get();fetch_threads.push_back(std::move(thread));
 ctx->thread=std::thread([=]{current_task=ctx;current_fetch_task=&m->fetch;try{fetch_task(m);}catch(TaskExit&){};});return m;
}
void release_fetch(esp_gmf_afe_manager_handle_t m){{std::lock_guard lock(m->afe_data->mutex);m->afe_data->released=true;}m->afe_data->changed.notify_all();}
struct Ring {std::mutex mutex;std::vector<uint8_t> bytes;
 void reset(){std::lock_guard lock(mutex);bytes.clear();}
 size_t available(){std::lock_guard lock(mutex);return bytes.size();}
 size_t free(){return 128-available();}
 size_t write_without_replacement(const uint8_t *p,size_t n,int,bool){std::lock_guard lock(mutex);bytes.insert(bytes.end(),p,p+n);observed.notify_all();return n;}
};
struct esp_gmf_payload_t {uint8_t *buf=nullptr;size_t valid_size=0,buf_length=0;};
void diag_add(std::atomic<unsigned>&c,unsigned n=1){c.fetch_add(n);}
void update_peak_atomic(std::atomic<unsigned>&c,unsigned n){auto old=c.load();while(old<n&&!c.compare_exchange_weak(old,n)){};}
std::atomic<bool> fail_build{false};
struct Processor {virtual ~Processor()=default;virtual bool wants_background_input()const=0;};
struct EspAfe:Processor {
 using ReconfigureRequest=Request;static constexpr unsigned RECONFIGURE_TASK_STACK_BYTES=12288;
 std::atomic<bool> processing_active_{false},drain_request_{false},process_busy_{false},instance_ready_{false};
 std::atomic<bool> afe_stopped_{false},vad_enabled_{true},voice_present_{false};
 bool continuous_vad_=true;
 std::atomic<uint32_t> processing_request_{0},manager_epoch_{0};
 std::atomic<bool> activity_pending_{false};
 std::atomic<TaskHandle_t> process_drain_waiter_{nullptr};
 SemaphoreHandle_t config_mutex_=new std::mutex;esp_gmf_afe_manager_handle_t afe_manager_=nullptr;
 bool afe_pipeline_running_=false,afe_pipeline_paused_=false;int staged_input_samples_=0,fetch_chunksize_=4;
 std::unique_ptr<Ring> fetch_output_ring_=std::make_unique<Ring>();
 std::atomic<unsigned> feed_queue_frames_{0},fetch_queue_frames_{0},fetch_timeout_{0},fetch_ok_{0},fetch_queue_peak_{0},output_ring_drop_{0};
 std::atomic<float> ringbuf_free_pct_{1};
 QueueHandle_t reconfigure_queue_=nullptr;StaticQueue_t reconfigure_queue_struct_;uint8_t reconfigure_queue_storage_[sizeof(Request)]{};
 TaskHandle_t reconfigure_task_handle_=nullptr;
 std::atomic<bool> reconfigure_task_running_{false},reconfigure_busy_{false},last_reconfigure_ok_{true};
 BinarySemaphore *reconfigure_done_=nullptr;StaticSemaphore_t reconfigure_done_storage_;
 std::atomic<unsigned> mode_requests{0};bool failed=false;
 std::string get_mode_name()const{return "SR_LOW_COST";}
 bool request_reinit_by_name(const char*);
 struct AfeInstance {esp_gmf_afe_manager_handle_t manager=nullptr;};
 bool is_initialized()const{return instance_ready_.load();}
 void mark_failed(){failed=true;}
 void reset_output_prebuffer_(){}
 void drain_feed_input_ring_(){}
 void flush_pipeline_before_stop_(){}
 void release_runtime_buffers_(){fetch_output_ring_=std::make_unique<Ring>();}
 AfeInstance detach_instance_(){stop_pipeline_();instance_ready_=false;AfeInstance old{afe_manager_};afe_manager_=nullptr;return old;}
 void destroy_instance_(AfeInstance *i){if(i->manager){esp_gmf_afe_manager_destroy(i->manager);i->manager=nullptr;}}
 bool recreate_instance_(bool){assert(afe_manager_==nullptr);if(fail_build)return false;afe_manager_=create_manager();afe_manager_->vad_enabled=vad_enabled_;fetch_output_ring_=std::make_unique<Ring>();instance_ready_=true;return true;}
 bool reinit_by_name(const char*){++mode_requests;return true;}
 bool all_features_disabled_()const{return false;}
 void clear_process_busy_();void setup();
 void set_processing_active(bool);bool start_pipeline_();bool pause_pipeline_();void stop_pipeline_();
 bool set_vad_enabled_runtime_(bool);
 bool start_reconfigure_task_();static void reconfigure_task_trampoline(void*);void reconfigure_task_loop_();bool is_reconfigure_idle()const;
 bool quiesce_process_(TickType_t);bool retire_processing_session_();bool service_processing_request_();
 ~EspAfe();
 static int32_t manager_read_cb_(void*,int,void*,uint32_t){return 0;}
 static void manager_result_cb_(afe_fetch_result_t*,void*);
 void update_fetch_ring_free_pct_();esp_gmf_err_io_t gmf_output_release_(esp_gmf_payload_t*,int);
 // ADAPTER_HELPERS
};
struct ESPAudioStack {
 EspAfe *processor_;std::atomic<bool> processor_background_consumer_registered_{false};bool external_listener=false;
 explicit ESPAudioStack(EspAfe *p):processor_(p){}
 // Listener registry/hardware are host substitutes. The background-policy
 // reconciliation below is the production method, including its real token.
 bool register_mic_consumer(void *token){assert(token==this);if(!external_listener)processor_->set_processing_active(true);return true;}
 void unregister_mic_consumer(void *token){assert(token==this);if(!external_listener)processor_->set_processing_active(false);}
 void sync_processor_background_consumer_();
};
// PARENT_METHODS
// ADAPTER_METHODS
int main(int argc,char **argv){
 assert(argc==2);unsigned scenario=std::stoul(argv[1]);auto owner=std::make_unique<EspAfe>();auto &afe=*owner;
 if(scenario>=10&&scenario<=15){
  psram_present=scenario!=11;
  psram_failure=scenario==12?1:scenario==13?2:0;
  fail_worker=scenario==14||scenario==15;
  if(scenario==15){
   assert(!afe.start_reconfigure_task_()&&!afe.reconfigure_task_running_);
   assert(external_attempts==1&&internal_attempts==1&&workers==0&&live_external_stack==nullptr);
   fail_worker=false;
  }
  const bool started=afe.start_reconfigure_task_();
  if(scenario==14){
   assert(!started&&!afe.reconfigure_task_running_&&workers==0);
   assert(external_attempts==1&&internal_attempts==1&&live_external_stack==nullptr);
  }else{
   assert(started&&workers==1);
   const bool external_allowed=configSUPPORT_STATIC_ALLOCATION&&CONFIG_FREERTOS_TASK_CREATE_ALLOW_EXT_MEM&&psram_present;
   if(scenario==15){assert(external_attempts==2&&internal_attempts==1&&live_external_stack!=nullptr);}
   else if(external_allowed){assert(external_attempts==1);assert(internal_attempts==(psram_failure?1U:0U));}
   else{assert(external_attempts==0&&internal_attempts==1&&live_external_stack==nullptr);}
  }
  owner.reset();
  assert(live_external_stack==nullptr&&external_allocations==external_releases);
  assert(owners_deleted==(scenario==14?0U:1U));
  assert(caps_deletions==(scenario==14||!configSUPPORT_STATIC_ALLOCATION?0U:1U));
  return 0;
 }
 if(scenario>=16){
  afe.continuous_vad_=scenario!=19;afe.setup();assert(!afe.failed);
  ESPAudioStack parent(&afe);parent.external_listener=scenario==18||scenario==19;
  if(parent.external_listener)afe.set_processing_active(true);
  parent.sync_processor_background_consumer_();
  until([&]{return afe.afe_manager_&&afe.afe_manager_->afe_data->entered.load();});auto *old=afe.afe_manager_;
  assert(afe.set_vad_enabled_runtime_(false));parent.sync_processor_background_consumer_();
  if(scenario==18){
   assert(afe.processing_active_&&managers_created==1&&managers_destroyed==0&&!old->vad_enabled);
   assert(afe.set_vad_enabled_runtime_(true));parent.sync_processor_background_consumer_();
   assert(afe.processing_active_&&old->vad_enabled&&managers_created==1);release_fetch(old);
  }else{
   if(scenario==19){parent.external_listener=false;afe.set_processing_active(false);}
   until([&]{return destroying>=1;});
   if(scenario==16||scenario==19){release_fetch(old);until([&]{return managers_destroyed==1&&afe.is_reconfigure_idle();});}
   if(scenario==17){parent.external_listener=true;afe.set_processing_active(true);}
   // The existing fetch wait owns the config mutex. Accepting a new VAD
   // intent must not synchronously wait for that worker's retirement.
   auto command=std::async(std::launch::async,[&]{return afe.set_vad_enabled_runtime_(true);});
   assert(command.wait_for(std::chrono::milliseconds(100))==std::future_status::ready);
   assert(command.get()&&afe.vad_enabled_);parent.sync_processor_background_consumer_();
   if(scenario==20){assert(afe.set_vad_enabled_runtime_(false));parent.sync_processor_background_consumer_();}
   if(scenario==17||scenario==20)release_fetch(old);
   if(scenario==19||scenario==20){until([&]{return afe.is_reconfigure_idle();});assert(managers_created==1&&!afe.processing_active_&&!parent.processor_background_consumer_registered_);}
   else{until([&]{return managers_created==2&&afe.processing_active_.load();});auto *fresh=afe.afe_manager_;assert(fresh->vad_enabled);until([&]{return fresh->afe_data->entered.load();});release_fetch(fresh);}
  }
  owner.reset();assert(workers==1&&owners_deleted==1&&managers_created==managers_destroyed);
  for(auto &t:fetch_threads)if(t->thread.joinable())t->thread.join();return 0;
 }
 fail_worker=scenario==7;
 if(scenario==8)afe.set_processing_active(true);
 afe.setup();
 if(scenario==7){assert(afe.failed&&managers_created==1&&managers_destroyed==1&&workers==0);owner.reset();for(auto &t:fetch_threads)t->thread.join();return 0;}
 assert(!afe.failed);
 if(scenario==6){afe.set_processing_active(false);assert(managers_created==1&&!afe.processing_active_);owner.reset();assert(managers_destroyed==1&&owners_deleted==1);for(auto &t:fetch_threads)t->thread.join();return 0;}
 afe.set_processing_active(true);
 until([&]{return afe.afe_manager_&&afe.afe_manager_->afe_data->entered.load();});
 auto *old=afe.afe_manager_;
 if(scenario==2){for(unsigned n=0;n<100;n++)afe.set_processing_active(true);assert(managers_created==1);release_fetch(old);until([&]{return afe.fetch_output_ring_->available()==8;});
 }else{
  if(scenario==4||scenario==5)afe.process_busy_=true;
  afe.set_processing_active(false);assert(!afe.processing_active_);
  afe.set_processing_active(true);
  if(scenario==1){until([&]{return destroying>=1;});afe.set_processing_active(false);}
  if(scenario==9){until([&]{return destroying>=1;});for(unsigned i=0;i<100;++i){afe.set_processing_active(false);afe.set_processing_active(true);}assert(managers_created==1);}
  if(scenario==3)fail_build=true;
  if(scenario==4){until([&]{return afe.process_drain_waiter_.load()!=nullptr;});assert(afe.request_reinit_by_name("VC_HIGH_PERF"));afe.clear_process_busy_();}
  if(scenario==5){until([&]{return !afe.reconfigure_busy_.load()&&!afe.activity_pending_.load();});assert(!afe.last_reconfigure_ok_&&managers_destroyed==0&&managers_created==1);afe.clear_process_busy_();afe.set_processing_active(true);until([&]{return destroying>=1;});}
  release_fetch(old);
  until([&]{return completed_fetches>=1;});
  until([&]{return !afe.reconfigure_busy_.load()&&!afe.activity_pending_.load();});
  assert(!afe.voice_present_ && afe.fetch_output_ring_->available()==0 && "old AFE fetch must not enter the new acquisition");
  if(scenario==1){assert(managers_created==1&&managers_destroyed==1&&!afe.processing_active_);
  }else if(scenario==3){assert(managers_created==1&&managers_destroyed==1&&!afe.processing_active_);fail_build=false;afe.set_processing_active(true);
   until([&]{return managers_created==2&&afe.processing_active_.load();});
  }
  if(scenario!=1){
   until([&]{return managers_created==2&&afe.processing_active_.load();});
   auto *fresh=afe.afe_manager_;assert(fresh!=old||managers_destroyed==1);until([&]{return fresh->afe_data->entered.load();});release_fetch(fresh);
   until([&]{return afe.fetch_output_ring_->available()==8;});
   {std::lock_guard lock(afe.fetch_output_ring_->mutex);auto *pcm=reinterpret_cast<const int16_t*>(afe.fetch_output_ring_->bytes.data());assert(pcm[0]==555);}
  }
  if(scenario==4)assert(afe.mode_requests==1);
 }
 assert(workers<=1);
 if(afe.afe_manager_)release_fetch(afe.afe_manager_);
 owner.reset();
 assert(workers==1&&owners_deleted==1);
 assert(managers_created==managers_destroyed);
 for(auto &t:fetch_threads)if(t->thread.joinable())t->thread.join();
 std::printf("scenario=%u created=%u destroyed=%u owner_workers=%u\n",scenario,managers_created.load(),managers_destroyed.load(),workers.load());
}
