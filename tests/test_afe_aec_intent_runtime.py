"""AEC intent acknowledgement must precede admission of the current acquisition."""

from pathlib import Path
import resource
import subprocess

import pytest

from test_afe_rebuild_runtime import rebuild_source
from test_volume_reference_runtime import method

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def aec_intent_binary(tmp_path_factory):
    source = rebuild_source()
    source = source.replace(
        "#include <algorithm>",
        "#include <algorithm>\n#include <mutex>\n#include <thread>\n#include <functional>",
    )
    source = source.replace(
        "namespace esp_audio_stack {\nusing esphome",
        "std::mutex owner_mutex;\nstd::function<void()> before_owner_unlock,after_aec_apply;\n"
        "int forced_aec_ack=-100;unsigned aec_calls=0;\nnamespace esp_audio_stack {\nusing esphome",
        1,
    )
    source = source.replace(
        "struct ScopedLock {ScopedLock(void*,unsigned){}explicit operator bool()const{return true;}};",
        """struct ScopedLock {
 std::unique_lock<std::mutex> held;
 ScopedLock(void*,unsigned):held(owner_mutex){}
 ~ScopedLock(){if(before_owner_unlock){auto hook=std::move(before_owner_unlock);hook();}}
 explicit operator bool()const{return true;}
};""",
    )
    source = source.replace(
        'if(feature==ESP_AFE_FEATURE_AEC){if(failure=="aec")return -1;m->aec=enable;return int(enable);}',
        """if(feature==ESP_AFE_FEATURE_AEC){
 ++aec_calls;
 if(failure=="aec")return -1;
 if(forced_aec_ack!=-100){if(forced_aec_ack>=0)m->aec=forced_aec_ack;return forced_aec_ack;}
 m->aec=enable;if(after_aec_apply){auto hook=std::move(after_aec_apply);hook();}
 return int(enable);
}""",
    )
    source = source.replace(
        "struct EspAfe {",
        "struct EspAfe {\n std::atomic<bool> reconfigure_busy_{false};\n void set_processing_active(bool);",
        1,
    )
    adapter = (ROOT / "esphome/components/esp_afe/esp_afe.cpp").read_text()
    source += method(adapter, "void EspAfe::set_processing_active(")
    source += r'''
void frame(EspAfe&afe,bool admitted){
 int16_t input[]={101,202,303,404},output[4]{};
 afe.process(input,nullptr,output,1);
 assert(afe.feed_input_ring_->bytes==(admitted?sizeof(input):0));
 int16_t consumed[4]{};esp_gmf_payload_t payload{consumed,sizeof(consumed),0};
 const int ret=afe.gmf_input_acquire_(&payload,sizeof(consumed),0);
 if(admitted){assert(ret==ESP_GMF_IO_OK&&payload.valid_size==sizeof(input));
  assert(std::memcmp(input,consumed,sizeof(input))==0);
 }else{assert(ret==ESP_GMF_IO_ABORT&&payload.valid_size==0);
  for(auto sample:output)assert(sample==0);
 }
}
void other_thread_while_locked(std::function<void()> work){
 std::thread worker([&]{
  const bool unlocked=owner_mutex.try_lock();if(unlocked)owner_mutex.unlock();assert(!unlocked);
  work();
 });worker.join();
}
int main(int argc,char**argv){
 const int scenario=std::stoi(argv[1]);
 static_assert(sizeof(AecIntent)==2*sizeof(std::atomic<uint32_t>));
 static_assert(std::atomic<uint32_t>::is_always_lock_free);
 EspAfe afe;afe.ns_enabled_=true;afe.aec_enabled_=false;
 afe.processing_request_=3;afe.manager_epoch_=1;
 afe.last_spec_process_size_=4;afe.last_spec_fetch_size_=4;afe.last_spec_mic_ch_=1;
 if(scenario<=2){
  after_aec_apply=[&]{
   assert(!afe.afe_manager_->aec&&!afe.processing_owner_current_());
   other_thread_while_locked([&]{
    assert(afe.set_aec_enabled_runtime_(true));
    if(scenario==1)assert(afe.set_aec_enabled_runtime_(false));
    if(scenario==2)afe.set_processing_active(false);
   });
  };
  before_owner_unlock=[&]{
   assert(afe.is_initialized()&&!afe.drain_request_&&afe.frame_spec_revision_==0);
   assert(!afe.aec_enabled_.is_applied()&&!afe.accepting_processing_());
   other_thread_while_locked([&]{frame(afe,false);});
  };
  assert(afe.service_processing_request_());
  if(scenario==2){assert(!afe.accepting_processing_());frame(afe,false);}
  else{assert(afe.aec_enabled_.is_applied()&&afe.accepting_processing_());
   assert(afe.afe_manager_->aec==(scenario==0));frame(afe,true);
   assert(afe.starts==1&&aec_calls==(scenario==0?2u:1u));
  }
 }else{
  assert(afe.service_processing_request_());auto *manager=afe.afe_manager_;
  if(scenario==3){
   // A newer OFF request arrives while the owner applies the previous ON.
   afe.processing_active_=false;assert(afe.set_aec_enabled_runtime_(true));
   after_aec_apply=[&]{other_thread_while_locked([&]{assert(afe.set_aec_enabled_runtime_(false));});};
   assert(afe.service_processing_request_());
   assert(!afe.aec_enabled_&&afe.afe_manager_->aec&&!afe.aec_enabled_.is_applied());
   frame(afe,false);assert(afe.service_processing_request_());
   assert(afe.aec_enabled_.is_applied()&&!afe.afe_manager_->aec);frame(afe,true);
  }else if(scenario==4||scenario==5){
   afe.processing_active_=false;assert(afe.set_aec_enabled_runtime_(true));
   if(scenario==4)failure="aec";else forced_aec_ack=0;
   assert(!afe.service_processing_request_());assert(!afe.accepting_processing_());frame(afe,false);
   failure.clear();forced_aec_ack=-100;afe.activity_pending_=false;
   assert(afe.set_aec_enabled_runtime_(true)&&afe.activity_pending_);
   assert(afe.service_processing_request_());assert(afe.afe_manager_->aec);frame(afe,true);
  }else{
   const auto request=afe.aec_enabled_.snapshot();const unsigned calls=aec_calls,starts=afe.starts,wakes=notifications;
   assert(afe.set_aec_enabled_runtime_(false));
   assert(afe.aec_enabled_.snapshot()==request&&aec_calls==calls&&afe.starts==starts&&notifications==wakes);
   frame(afe,true);
  }
  assert(afe.afe_manager_==manager&&afe.frame_spec_revision_==0);
 }
 afe.cleanup();assert(live_allocations==0);
}
'''
    directory = tmp_path_factory.mktemp("aec-intent")
    cpp = directory / "intent.cpp"
    cpp.write_text(source)
    binary = directory / "intent"
    built = subprocess.run(
        ["g++", "-std=c++17", "-O1", "-g", "-pthread", "-fsanitize=address,undefined",
         str(cpp), "-o", str(binary)], capture_output=True, text=True,
    )
    assert built.returncode == 0, built.stderr
    return binary


@pytest.mark.parametrize("scenario", range(7), ids=[
    "ON-during-install", "OFF-ON-OFF-during-install", "epoch-cancel-during-install",
    "stale-ack", "failed-ack", "discordant-ack", "already-applied-no-op",
])
def test_aec_latest_ack_precedes_valid_feed(aec_intent_binary, scenario):
    result = subprocess.run(
        [str(aec_intent_binary), str(scenario)], capture_output=True, text=True, timeout=8,
        preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_CORE, (0, 0)),
    )
    assert result.returncode == 0, result.stdout + result.stderr
