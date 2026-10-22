"""Production speaker admission must honor the producer's wait budget on rejection."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1] / 'esphome/components/esp_audio_stack'


def method(text, signature):
    start = text.index(signature)
    opening = text.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[start:end]


def test_rejected_output_yields_without_accepting_or_dropping_pcm(tmp_path):
    adapter = method((ROOT / 'speaker/esp_audio_stack_speaker.cpp').read_text(),
                     'size_t ESPAudioStackSpeaker::play(const uint8_t *data, size_t length, TickType_t ticks_to_wait)')
    stack = method((ROOT / 'esp_audio_stack.cpp').read_text(), 'size_t ESPAudioStack::play(')
    code = r'''
#include <atomic>
#include <algorithm>
#include <cassert>
#include <cstdint>
#include <cstddef>
using TickType_t=unsigned;
unsigned waited=0;
void vTaskDelay(unsigned ticks){waited+=ticks;}
unsigned millis(){return 123;}
struct Buffer {
 unsigned writes=0;
 size_t free() const {return 4096;}
 size_t write_without_replacement(const void*,size_t length,unsigned,bool){++writes;return length;}
};
struct ESPAudioStack {
 Buffer *speaker_buffer_=nullptr;std::atomic<bool> request_speaker_reset_{false};
 std::atomic<bool> has_i2s_error_{false};
 bool has_failed_session()const{return false;}
 void start(){}
 unsigned get_speaker_channels(){return 1;}
 size_t play(const uint8_t*,size_t,TickType_t);
};
namespace speaker {enum {STATE_STOPPED,STATE_RUNNING,STATE_STARTING};}
struct ESPAudioStackSpeaker {
 std::atomic<bool> implicit_recovery_used_{false};
 void enable_loop_soon_any_context(){}
 bool failed=false;int state_=speaker::STATE_RUNNING;
 std::atomic<bool> listener_registered_{true};unsigned last_write_ms_=0;
 ESPAudioStack *parent_;bool is_failed(){return failed;}
 void start(){listener_registered_=true;state_=speaker::STATE_STARTING;}
 size_t play(const uint8_t*,size_t,TickType_t);
};
''' + stack + '\n' + adapter + r'''
int main(){
 ESPAudioStack stack;Buffer buffer;stack.speaker_buffer_=&buffer;
 ESPAudioStackSpeaker sink;sink.parent_=&stack;uint8_t pcm[64]{};
 // The output was stopped while a mixer task still has an exposed buffer.
 sink.listener_registered_=false;
 assert(sink.play(pcm,sizeof(pcm),25)==0);
 assert(waited==25 && buffer.writes==0);
 // Audio task reset is another temporarily unavailable sink, not a busy loop.
 waited=0;sink.listener_registered_=true;stack.request_speaker_reset_=true;
 assert(sink.play(pcm,sizeof(pcm),25)==0);
 assert(waited==25 && buffer.writes==0);
 // Nonblocking callers remain nonblocking; normal playback gets no added wait.
 waited=0;assert(sink.play(pcm,sizeof(pcm),0)==0);assert(waited==0);
 stack.request_speaker_reset_=false;
 assert(sink.play(pcm,sizeof(pcm),25)==sizeof(pcm));
 assert(waited==0 && buffer.writes==1 && sink.last_write_ms_==123);
}
'''
    source = tmp_path / 'backpressure.cpp'
    source.write_text(code)
    binary = tmp_path / 'backpressure'
    subprocess.run(['g++','-std=c++17',str(source),'-o',str(binary)],check=True,capture_output=True,text=True)
    result=subprocess.run(['bash','-c','ulimit -c 0; exec "$1"','bash',str(binary)],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
