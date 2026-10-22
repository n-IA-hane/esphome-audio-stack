#include "host.h"
#include <csignal>
#include <cstdlib>
#include <map>
#define protected public
#include "capture.h"
#undef protected
#include "capture.cpp"
using namespace esphome::esp_audio_capture;
using namespace esphome::esp_audio_stack;
void spin(ESPAudioCapture &c,std::function<bool()> until){
 auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(5);
 while(!until()){c.loop();assert(std::chrono::steady_clock::now()<deadline);std::this_thread::sleep_for(std::chrono::milliseconds(1));}
}
int main(int argc,char**argv){
 assert(argc==3);signal(SIGPIPE,SIG_IGN);const std::string scenario=argv[2];
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
