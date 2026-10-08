"""Exercise the production stop/write/reset ordering without hardware scheduling."""
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
AUDIO_SOURCE = ROOT / 'esphome/components/esp_audio_stack/esp_audio_stack.cpp'


def _method(text, signature):
    start = text.index(signature)
    opening = text.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[start:end]


def test_new_pcm_is_not_accepted_into_a_pending_reset(tmp_path):
    source = AUDIO_SOURCE.read_text()
    code = r'''
#include <atomic>
#include <cassert>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <functional>
using TickType_t=unsigned;
#define ESP_LOGI(...) ((void)0)
struct Buffer {
 unsigned queued=100,discarded=0;std::function<void()> during_reset;
 void reset(){if(during_reset)during_reset();discarded+=queued;queued=0;}
 size_t write_without_replacement(void*,size_t len,unsigned,bool){queued+=len;return len;}
};
struct Callback {void call(){}};
struct ESPAudioStack {
 std::atomic<bool> speaker_running_{true},has_mic_consumers_{true},request_speaker_reset_{false};
 Callback speaker_idle_callback_;Buffer buffer;Buffer *speaker_buffer_=&buffer;
 void update_runtime_state_(){}void stop(){}unsigned get_speaker_channels(){return 1;}
 void stop_speaker();void service_speaker_reset_();
 size_t play(const uint8_t*,size_t,TickType_t);
};
''' + '\n'.join(_method(source, name) for name in (
        'void ESPAudioStack::stop_speaker()',
        'void ESPAudioStack::service_speaker_reset_()',
        'size_t ESPAudioStack::play(',
    )) + r'''
int main(){
 ESPAudioStack stack;
 stack.stop_speaker();
 assert(stack.request_speaker_reset_);
 // The speaker adapter marks itself STOPPED immediately after stop_speaker.
 // A new producer can therefore enter play before the audio task services it.
 uint8_t samples[64]{};
 auto accepted=stack.play(samples,sizeof(samples),0);
 stack.service_speaker_reset_();
 std::fprintf(stderr,"accepted_new=%zu retained_new=%u discarded_total=%u\n",accepted,stack.buffer.queued,stack.buffer.discarded);
 assert(accepted==0 || stack.buffer.queued>=accepted);
 // Once reset completes, retrying the same caller-owned data must succeed.
 assert(stack.play(samples,sizeof(samples),0)==sizeof(samples));
 assert(stack.buffer.queued>=sizeof(samples));
 // Keep the admission barrier set during reset itself, not only before it.
 stack.stop_speaker();size_t accepted_during=999;
 stack.buffer.during_reset=[&]{
   stack.stop_speaker();  // A second stop coalesces while admission is closed.
   accepted_during=stack.play(samples,sizeof(samples),0);
 };
 stack.service_speaker_reset_();assert(accepted_during==0);
 stack.buffer.during_reset={};
 assert(stack.play(samples,sizeof(samples),0)==sizeof(samples));
}
'''
    cpp = tmp_path / 'reset.cpp'
    binary = tmp_path / 'reset'
    cpp.write_text(code)
    subprocess.run(['g++','-std=c++17',str(cpp),'-o',str(binary)],check=True,capture_output=True,text=True)
    run=subprocess.run(['bash','-c','ulimit -c 0; exec "$1"','bash',str(binary)],capture_output=True,text=True)
    assert run.returncode==0,run.stderr
