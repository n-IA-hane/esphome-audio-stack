"""Compile the actual opt-in wrappers, with explicit vendor/task substitutes."""
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("enabled", [False, True])
def test_boundaries_delegate_without_changing_audio_and_compile_out(tmp_path, enabled):
    component = ROOT / "esphome/components/esp_audio_capture"
    header = (component / "boundary_probe.h").read_text()
    source = (component / "boundary_probe.cpp").read_text()
    code = "\n".join(line for line in (header + '\n' + source).splitlines()
                     if not line.startswith('#include') and line != '#pragma once')
    prelude = r'''
#include <atomic>
#include <cassert>
#include <cstdint>
#include <cstddef>
#include <cstring>
#include <mutex>
#include <vector>
#define USE_ESP32
struct afe_config_t{};struct esp_afe_sr_data_t{};
struct esp_afe_sr_iface_t {
 esp_afe_sr_data_t*(*create_from_config)(afe_config_t*);
 int(*feed)(esp_afe_sr_data_t*,const int16_t*);
 void(*destroy)(esp_afe_sr_data_t*);
 int(*get_feed_chunksize)(esp_afe_sr_data_t*);
 int(*get_feed_channel_num)(esp_afe_sr_data_t*);
};
struct FrontendState{};struct FrontendOutput{size_t size;};
using portMUX_TYPE=std::mutex;
#define portMUX_INITIALIZER_UNLOCKED {}
#define portENTER_CRITICAL(x) ((x)->lock())
#define portEXIT_CRITICAL(x) ((x)->unlock())
using TaskHandle_t=const char*;const char* task_name="mww";
TaskHandle_t xTaskGetCurrentTaskHandle(){return task_name;}
const char* pcTaskGetName(TaskHandle_t h){return h;}
uint64_t esp_timer_get_time(){static uint64_t time=0;return ++time;}
int feeds=0,frontends=0,destroys=0;bool mutate=false;
esp_afe_sr_data_t data;
esp_afe_sr_data_t* create(afe_config_t*){return &data;}
int feed(esp_afe_sr_data_t*,const int16_t* p){++feeds;if(mutate)const_cast<int16_t*>(p)[0]+=1;return 73;}
void destroy(esp_afe_sr_data_t*){++destroys;}
int frames(esp_afe_sr_data_t*){return 2;}int channels(esp_afe_sr_data_t*){return 3;}
esp_afe_sr_iface_t iface{create,feed,destroy,frames,channels};
extern "C" const esp_afe_sr_iface_t* __real_esp_afe_handle_from_config(const afe_config_t*){return &iface;}
extern "C" FrontendOutput __real_FrontendProcessSamples(FrontendState*,const int16_t*,size_t count,size_t* used){
 ++frontends;*used=count>2?2:count;return {1};
}
'''
    checks = r'''
using namespace esphome::esp_audio_capture;
struct Sink:BoundarySink {
 uint32_t active=0;std::vector<BoundaryRecord> records;std::vector<int16_t> last;
 uint32_t boundary_session()const override{return active;}
 void on_boundary(const BoundaryRecord&r,const int16_t*pcm)override{
  records.push_back(r);last.assign(pcm,pcm+r.frames*r.channels);
 }
};
int main(){
 Sink sink;assert(boundary_register_sink(&sink));Sink other;assert(!boundary_register_sink(&other));
 afe_config_t cfg;auto* proxy=__wrap_esp_afe_handle_from_config(&cfg);assert(proxy!=&iface);
 auto* instance=proxy->create_from_config(&cfg);int16_t pcm[]={1,2,3,4,5,6};
 assert(proxy->feed(instance,pcm)==73&&feeds==1&&sink.records.empty());
 sink.active=1;assert(proxy->feed(instance,pcm)==73&&feeds==2);
 assert(sink.records.back().point==BOUNDARY_FEED&&sink.records.back().frames==2);
 assert(sink.records.back().result==73&&sink.records.back().flags==0);
 assert((sink.last==std::vector<int16_t>{1,2,3,4,5,6}));
 mutate=true;proxy->feed(instance,pcm);assert(sink.records.back().flags==1);mutate=false;
 FrontendState state;size_t consumed=0;auto result=__wrap_FrontendProcessSamples(&state,pcm,6,&consumed);
 assert(result.size==1&&consumed==2&&frontends==1);
 assert(sink.records.back().point==BOUNDARY_FRONTEND&&sink.records.back().frames==2&&sink.last.size()==2);
 assert(pcm[0]==2&&pcm[1]==2&&pcm[5]==6);auto size=sink.records.size();
 task_name="other";__wrap_FrontendProcessSamples(&state,pcm,6,&consumed);
 assert(frontends==2&&sink.records.size()==size&&boundary_health().foreign_frontend==1);
 sink.active=0;task_name="mww";__wrap_FrontendProcessSamples(&state,pcm,6,&consumed);
 assert(frontends==3&&sink.records.size()==size);
 proxy->destroy(instance);assert(destroys==1);
}
'''
    cpp = tmp_path / "boundary.cpp"
    cpp.write_text(prelude + ("\n#define USE_ESP_AUDIO_BOUNDARY_PROBES\n" if enabled else "\n")
                   + code + (checks if enabled else "\nint main(){return 0;}\n"))
    binary = tmp_path / "boundary"
    result = subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
        "-fsanitize=address,undefined", str(cpp), "-o", str(binary)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    subprocess.run([str(binary)], check=True)
    if not enabled:
        symbols = subprocess.check_output(["nm", "-C", str(binary)], text=True)
        assert "esp_audio_capture::" not in symbols
