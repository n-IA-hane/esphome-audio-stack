#include "host.h"
#include <csignal>
#include <cstdlib>
#include <map>
#include <arpa/inet.h>
#include <fcntl.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <unistd.h>
#define protected public
#include "capture.h"
#undef protected

std::string send_scenario;
std::mutex send_gate_mutex;
std::condition_variable send_gate_cv;
bool send_gate_entered=false,send_gate_release=false;
unsigned send_calls=0;
std::atomic<bool> send_blocked=false;
std::atomic<int64_t> record_wait_budget_us=0;

// The network adapter can return a legal short write at either record part.
// Production deadline, main-loop stop and worker methods remain unchanged.
ssize_t capture_send(int fd,const void*data,size_t bytes,int flags){
 const auto call=++send_calls;
 if(send_scenario=="stalled-header"&&call>1){send_blocked=true;errno=EAGAIN;return -1;}
 const bool header=send_scenario.find("header")!=std::string::npos&&call==1;
 const bool payload=send_scenario.find("payload")!=std::string::npos&&call==2;
 if(header||payload){
  const auto sent=::send(fd,data,std::min<size_t>(17,bytes),flags);assert(sent==17);
  if(send_scenario!="stalled-header"){
   std::unique_lock lock(send_gate_mutex);send_gate_entered=true;send_gate_cv.notify_all();
   send_gate_cv.wait(lock,[]{return send_gate_release;});
  }
  return sent;
 }
 return ::send(fd,data,bytes,flags);
}
int capture_select(int nfds,fd_set*read,fd_set*write,fd_set*error,timeval*timeout){
 if(send_blocked){
  assert(timeout);record_wait_budget_us=int64_t(timeout->tv_sec)*1000000+timeout->tv_usec;
  // A blocked peer never becomes writable. Wait only the exact finite budget.
  return ::select(0,nullptr,nullptr,nullptr,timeout);
 }
 return ::select(nfds,read,write,error,timeout);
}
#define send capture_send
#define select capture_select
#include "capture.cpp"
#undef select
#undef send
using namespace esphome::esp_audio_capture;
using namespace esphome::esp_audio_stack;
void spin(ESPAudioCapture &c,std::function<bool()> until){
 auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(5);
 while(!until()){c.loop();assert(std::chrono::steady_clock::now()<deadline);std::this_thread::sleep_for(std::chrono::milliseconds(1));}
}
int main(int argc,char**argv){
 assert(argc==3);signal(SIGPIPE,SIG_IGN);const std::string scenario=argv[2];send_scenario=scenario;
 const bool terminal=scenario.find("header")!=std::string::npos||scenario.find("payload")!=std::string::npos;
 if(terminal)capture_clock_override_us=1000000;
 ESPAudioStack parent;ESPAudioStackMicrophone microphone;ESPAudioCapture capture;
 if(scenario=="saturated")microphone.listeners=16;
 const auto original_listeners=microphone.listeners;
 if(scenario=="registry-full")parent.admit=false;
 capture.set_parent(&parent);capture.set_microphone(&microphone);capture.set_address("127.0.0.1");
 capture.set_port(std::atoi(argv[1]));capture.set_points(31);capture.set_duration(350);capture.setup();
 assert(!queues&&!task_count);assert(capture.start());assert(!capture.start());
 if(scenario=="unreachable"||scenario=="registry-full") {
  spin(capture,[&]{return capture.worker_handle_==nullptr;});
  assert(!queues&&!task_count&&microphone.listeners==1&&microphone.starts==0&&parent.consumers==1);return 0;
 }
 spin(capture,[&]{return capture.active_||capture.worker_handle_==nullptr;});assert(capture.active_);
 assert(microphone.listeners==original_listeners&&parent.consumers==2);
 if(terminal){
  if(scenario.find("deadline")==0)capture_clock_override_us=capture.deadline_us_-10000;
  std::vector<int16_t> pcm(256,123);
  CaptureFrame final_frame{CapturePoint::OUTPUT,pcm.data(),pcm.size(),16000,parent.generation,123,16,1,1,{0,0}};
  capture.on_capture_frame(final_frame);
  if(scenario!="stalled-header"){
   {std::unique_lock lock(send_gate_mutex);assert(send_gate_cv.wait_for(lock,std::chrono::seconds(5),[]{return send_gate_entered;}));}
   if(scenario.find("deadline")==0){capture_clock_override_us=capture.deadline_us_+1;capture.loop();}
   else if(scenario=="mute-payload"){microphone.muted=true;capture.loop();}
   else if(scenario=="shutdown-header")capture.on_shutdown();
   else capture.stop();
   {std::lock_guard lock(send_gate_mutex);send_gate_release=true;send_gate_cv.notify_all();}
  }
  spin(capture,[&]{return capture.worker_handle_==nullptr;});
  assert(!capture.active_&&!queues&&!task_count&&parent.consumers==1);
  if(scenario=="stalled-header"){assert(record_wait_budget_us==1000000);puts("record_wait_budget_us=1000000");}
  puts("terminal record and cleanup checked");return 0;
 }
 int32_t raw[]={100<<16,900<<16,-200*65536,800<<16,300<<16,700<<16};
 CaptureFrame frame{CapturePoint::RAW_MIC,raw,3,48000,10,123,32,2,2,{0,1}};
 capture.on_capture_frame(frame);
 int16_t processed[]={100,-200,300};frame.data=processed;frame.bits=16;frame.channels=1;frame.stride=1;frame.sample_rate=16000;
 for(unsigned p=1;p<5;++p){frame.point=static_cast<CapturePoint>(p);capture.on_capture_frame(frame);}
 if(scenario=="inflight") {
  auto *queue=capture.queue_.get();queue->pause_next=true;
  std::thread producer([&]{capture.on_capture_frame(frame);});
  {std::unique_lock lock(queue->gate_mutex);queue->gate_cv.wait(lock,[&]{return queue->entered;});}
  capture.stop();
  spin(capture,[&]{std::lock_guard lock(*capture.socket_mutex_);return capture.socket_fd_==-1;});
  assert(!capture.worker_done_&&capture.writers_==1&&queues==1&&task_count==1);
  {std::lock_guard lock(queue->gate_mutex);queue->released=true;queue->gate_cv.notify_all();}
  producer.join();
 }
 if(scenario=="mute") {
  microphone.muted=true;const auto before=capture.positions_[4];capture.on_capture_frame(frame);
  assert(!capture.active_&&capture.positions_[4]==before);capture.loop();
 }
 if(scenario=="generation") {++parent.generation;capture.loop();assert(!capture.active_);}
 if(scenario=="format") {frame.sample_rate=8000;capture.on_capture_frame(frame);assert(!capture.active_);}
 if(scenario=="overflow"){
  std::vector<int16_t> big(4096,123);frame.data=big.data();frame.frames=big.size();
  for(unsigned i=0;i<20000&&capture.active_;++i)capture.on_capture_frame(frame);
  assert(capture.drops_[4]>0);assert(capture.queue_->peak<=65536);
  capture.stop();
 }
 spin(capture,[&]{return capture.worker_handle_==nullptr;});
 assert(!capture.active_&&!queues&&!task_count&&microphone.listeners==original_listeners&&microphone.starts==microphone.stops&&parent.consumers==1);
 // An already scheduled callback after retirement cannot touch a freed queue.
 capture.on_capture_frame(frame);
 if(scenario=="reuse"){
  assert(capture.start());spin(capture,[&]{return capture.active_||capture.worker_handle_==nullptr;});
  if(capture.active_)capture.stop();spin(capture,[&]{return capture.worker_handle_==nullptr;});
  assert(!queues&&!task_count&&microphone.listeners==1);
 }
 puts("capture ownership and cleanup passed");
}
