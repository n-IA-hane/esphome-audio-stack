"""Exercise production play() with ESPHome RingBuffer and bounded BYTEBUF I/O.

Only the FreeRTOS storage/wait adapter is simulated. The actual RingBuffer
implementation performs writes, reads and wrap handling. This is a PCM contract
check, not a FreeRTOS scheduling or physical audio qualification.
"""
from pathlib import Path
import subprocess

from speaker_gate_host import SPEAKER_GATE_FLAGS

import esphome
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def partial_writer(tmp_path_factory):
    directory = tmp_path_factory.mktemp('speaker-partial')
    stubs = directory / 'stubs'
    files = {
        'freertos/FreeRTOS.h': r'''
#pragma once
using TickType_t = unsigned;
using BaseType_t = int;
using UBaseType_t = unsigned;
constexpr int pdPASS = 1;
constexpr int RINGBUF_TYPE_BYTEBUF = 0;
void vTaskDelay(TickType_t);
''',
        'freertos/ringbuf.h': r'''
#pragma once
#include "FreeRTOS.h"
#include <algorithm>
#include <cassert>
#include <cstring>
#include <functional>
#include <cstddef>
#include <cstdint>
extern unsigned waited;
extern std::function<void()> wait_hook;
struct FakeRing {
  uint8_t *storage; size_t capacity, read=0, write=0, queued=0, borrowed=0;
};
using RingbufHandle_t = FakeRing *;
using StaticRingbuffer_t = FakeRing;
inline RingbufHandle_t xRingbufferCreateStatic(size_t size, int, uint8_t *storage, StaticRingbuffer_t *structure) {
  *structure=FakeRing{storage,size};return structure;
}
inline void vRingbufferDelete(RingbufHandle_t) {}
inline size_t xRingbufferGetCurFreeSize(RingbufHandle_t ring) {
  return ring->capacity-ring->queued-ring->borrowed;
}
inline bool xRingbufferSend(RingbufHandle_t ring, const void *data, size_t size, TickType_t ticks) {
  if(size>ring->capacity) return false;
  if(size>xRingbufferGetCurFreeSize(ring) && ticks) {
    waited+=ticks;
    if(wait_hook) wait_hook();
  }
  if(size>xRingbufferGetCurFreeSize(ring)) return false;
  const auto *bytes=static_cast<const uint8_t *>(data);
  for(size_t i=0;i<size;++i) ring->storage[(ring->write+i)%ring->capacity]=bytes[i];
  ring->write=(ring->write+size)%ring->capacity; ring->queued+=size;
  return true;
}
inline void *xRingbufferReceiveUpTo(RingbufHandle_t ring,size_t *size,TickType_t,size_t limit) {
  assert(ring->borrowed==0);
  *size=std::min({limit,ring->queued,ring->capacity-ring->read});
  if(!*size) return nullptr;
  auto *result=ring->storage+ring->read;
  ring->read=(ring->read+*size)%ring->capacity;
  ring->queued-=*size; ring->borrowed=*size;
  return result;
}
inline void vRingbufferReturnItem(RingbufHandle_t ring,void*) { ring->borrowed=0; }
inline void vRingbufferGetInfo(RingbufHandle_t ring,void*,void*,void*,void*,UBaseType_t *queued) {
  *queued=ring->queued;
}
''',
        'esphome/core/helpers.h': r'''
#pragma once
#include <memory>
#include <cstdint>
namespace esphome {
using std::make_unique;
template<typename T> struct RAMAllocator {
  static constexpr int PREFER_INTERNAL=1, NONE=0;
  explicit RAMAllocator(int=0) {}
  T *allocate(size_t count) { return new T[count]; }
  void deallocate(T *data,size_t) { delete[] data; }
};
}
''',
        'esphome/core/log.h': '#pragma once\n#define ESPHOME_LOG_TAG(name, tag) [[maybe_unused]] static constexpr const char *const name = tag\n#define ESP_LOGD(...) ((void)0)\n',
    }
    for name, content in files.items():
        path = stubs / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    source = (ROOT / 'esphome/components/esp_audio_stack/esp_audio_stack.cpp').read_text()
    start = source.index('size_t ESPAudioStack::play(')
    end, depth = source.index('{', start) + 1, 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    body = source[start:end]
    from test_speaker_backpressure_runtime import method
    facade = (ROOT / 'esphome/components/esp_audio_stack/speaker/esp_audio_stack_speaker.cpp').read_text()
    body += '\n' + method(source, 'size_t ESPAudioStack::play_with_admission_(')
    lifecycle = method(source, 'void ESPAudioStack::service_speaker_reset_()')
    header = (ROOT / 'esphome/components/esp_audio_stack/esp_audio_stack.h').read_text()
    set_error = method(header, 'void set_i2s_error_(')
    adapter_methods = '\n'.join(method(facade, signature) for signature in (
        'void ESPAudioStackSpeaker::stop()', 'void ESPAudioStackSpeaker::finish()',
        'size_t ESPAudioStackSpeaker::play(const uint8_t *data, size_t length, TickType_t ticks_to_wait)',
    ))
    harness = r'''
#include <atomic>
#include <array>
#include <chrono>
#include <condition_variable>
#include <thread>
#include <vector>
#include <string>
#include <cstdlib>
#include "freertos/ringbuf.h"
#include "esphome/components/ring_buffer/ring_buffer.h"
unsigned waited=0;
std::function<void()> wait_hook;
void vTaskDelay(TickType_t ticks){waited+=ticks;}
using esphome::ring_buffer::RingBuffer;
struct ESPAudioStack {
  RingBuffer *speaker_buffer_;
  std::atomic<bool> request_speaker_reset_{false};
  unsigned channels;
  std::atomic<bool> has_i2s_error_{false};
  HostSpeakerGate speaker_write_gate_;
  std::atomic<uint32_t> tx_playback_generation_{0};
  void notify_speaker_writers_(bool cancel=false) { speaker_write_gate_.notify(cancel); }
  bool has_failed_session() const { return has_i2s_error_; }
  void start() {}
  void service_speaker_reset_();
  void reset_tx_playback_() { ++tx_playback_generation_; }
  // PRODUCTION_SET_ERROR
  unsigned get_speaker_channels() const { return channels; }
  size_t play(const uint8_t*,size_t,TickType_t);
  size_t play_with_admission_(const uint8_t*,size_t,TickType_t, const std::atomic<SpeakerAdmission> *);
};
struct ESPAudioStackSpeaker {
  ESPAudioStack *parent_;
  std::atomic<SpeakerAdmission> admission_{SpeakerAdmission::READY};
  std::atomic<bool> listener_registered_{true}, implicit_recovery_used_{false};
  std::atomic<uint32_t> last_write_ms_{0};
  bool pause_state_{false};
  bool is_failed() const { return false; }
  void enable_loop_soon_any_context() {}
  void set_pause_state(bool value) { pause_state_ = value; }
  void start() { listener_registered_ = true; admission_ = SpeakerAdmission::STARTING; }
  void stop(); void finish();
  size_t play(const uint8_t *, size_t, TickType_t);
};
unsigned millis() { return xTaskGetTickCount(); }
// PRODUCTION_PLAY
std::vector<uint8_t> sequence(size_t size,unsigned seed=0) {
  std::vector<uint8_t> result(size);
  for(size_t i=0;i<size;++i) result[i]=(i*37+seed)%251;
  return result;
}
std::vector<uint8_t> drain(RingBuffer &ring,size_t limit) {
  std::vector<uint8_t> result(limit);
  result.resize(ring.read(result.data(),result.size(),0)); return result;
}
// CONCURRENT_CASES
int main(int argc,char **argv) {
  assert(argc==3); std::string scenario=argv[1]; unsigned channels=std::atoi(argv[2]);
  const size_t frame=channels*sizeof(int16_t);
  const size_t capacity=scenario=="device_sized_wait" ? 3200*channels : 5*frame+(scenario=="wrap" ? 1 : 0);
  std::shared_ptr<RingBuffer> ring=RingBuffer::create(capacity);
  ESPAudioStack stack{ring.get(),false,channels};
  speaker_wait_hook=[&](unsigned ticks){waited+=ticks;if(wait_hook)wait_hook();};
  auto play=[&](const uint8_t *data,size_t length,TickType_t ticks) {
    return stack.play(data,length,ticks);
  };
  auto input=sequence(17*frame+1,101);
  if(scenario=="reset" || scenario=="stop" || scenario=="finish" || scenario=="error" ||
     scenario=="space" || scenario=="ready") {
    concurrent(scenario, stack, *ring, frame, capacity);
  } else if(scenario=="timeout_budget") {
    auto prefix = sequence(capacity, 14);
    assert(ring->write_without_replacement(prefix.data(), capacity, 0, false) == capacity);
    use_speaker_test_clock = true;
    speaker_test_ticks = UINT32_MAX - 4;
    std::vector<TickType_t> budgets;
    speaker_wait_hook = [&](TickType_t ticks) {
      budgets.push_back(ticks);
      speaker_test_ticks += 3;
      stack.notify_speaker_writers_();  // progress notification without enough space for this writer
    };
    assert(stack.play(input.data(), frame, 7) == 0);
    assert((budgets == std::vector<TickType_t>{7, 4, 1}));
    assert(drain(*ring, capacity) == prefix);
  } else if(scenario=="main_task") {
    // This call runs on the task which owns the facade readiness transition.
    ESPAudioStackSpeaker sink{&stack};
    sink.admission_ = SpeakerAdmission::STARTING;
    assert(sink.play(input.data(), frame, portMAX_DELAY)==0 && waited==0);
    sink.admission_ = SpeakerAdmission::READY;
    assert(sink.play(input.data(), frame, portMAX_DELAY)==frame);
    assert(drain(*ring,capacity).size()==frame);
  } else if(scenario=="terminal") {
    stack.set_i2s_error_(true);
    assert(stack.play(input.data(),frame,portMAX_DELAY)==0);
  } else if(scenario=="oversized") {
    const size_t total=17*frame; size_t offset=0; std::vector<uint8_t> heard;
    while(offset<total) {
      size_t written=play(input.data()+offset,total-offset,7);
      assert(written>0 && written<=capacity && written%frame==0);
      offset+=written; auto chunk=drain(*ring,capacity);heard.insert(heard.end(),chunk.begin(),chunk.end());
    }
    assert(heard==std::vector<uint8_t>(input.begin(),input.begin()+total));
  } else if(scenario=="almost_full" || scenario=="wrap") {
    auto prefix=sequence(4*frame);
    assert(ring->write_without_replacement(prefix.data(),prefix.size(),0,false)==prefix.size());
    size_t removed=scenario=="wrap" ? 2*frame : 0;
    if(removed) assert(drain(*ring,removed)==std::vector<uint8_t>(prefix.begin(),prefix.begin()+removed));
    const size_t expected=(capacity-(prefix.size()-removed))/frame*frame;
    size_t written=play(input.data(),5*frame,0);
    assert(written==expected && written%frame==0);
    std::vector<uint8_t> want(prefix.begin()+removed,prefix.end());want.insert(want.end(),input.begin(),input.begin()+written);
    assert(drain(*ring,capacity)==want);
  } else if(scenario=="wait" || scenario=="no_space" || scenario=="device_sized_wait" || scenario=="wait_timeout") {
    auto prefix=sequence(capacity);
    assert(ring->write_without_replacement(prefix.data(),prefix.size(),0,false)==capacity);
    std::vector<uint8_t> consumed;
    if(scenario!="wait_timeout") wait_hook=[&]{consumed=drain(*ring,frame);stack.notify_speaker_writers_();};
    if(scenario=="device_sized_wait") input=sequence(8192);
    size_t written=play(input.data(),input.size(),scenario=="no_space" ? 0 : 7);
    if(scenario=="wait" || scenario=="device_sized_wait") {
      const size_t expected=frame;
      assert(written==expected && waited==7);
      assert(consumed==std::vector<uint8_t>(prefix.begin(),prefix.begin()+frame));
      std::vector<uint8_t> want(prefix.begin()+frame,prefix.end());want.insert(want.end(),input.begin(),input.begin()+expected);
      assert(drain(*ring,capacity)==want);
    } else if(scenario=="wait_timeout") {
      assert(written==0 && waited==7 && consumed.empty());
      assert(drain(*ring,capacity)==prefix);
    } else {
      assert(written==0 && waited==0 && consumed.empty());
      assert(drain(*ring,capacity)==prefix);
    }
  } else if(scenario=="unaligned") {
    assert(play(input.data(),2*frame+1,0)==2*frame);
    assert(play(input.data()+2*frame,1,0)==0);
    assert(drain(*ring,capacity)==std::vector<uint8_t>(input.begin(),input.begin()+2*frame));
  } else { assert(false); }
  assert(ring->available()==0);
}
'''.replace('// PRODUCTION_PLAY', body + '\n' + lifecycle + '\n' + adapter_methods)
    harness = harness.replace('// PRODUCTION_SET_ERROR', set_error).replace(
        '// CONCURRENT_CASES', (ROOT / 'tests/fixtures/speaker_sync/concurrent.cpp.in').read_text()
    )
    unit = directory / 'partial.cpp'
    unit.write_text(harness)
    binary = directory / 'partial'
    esphome_root = Path(esphome.__file__).resolve().parent
    ring = esphome_root / 'components/ring_buffer/ring_buffer.cpp'
    compiled = subprocess.run(['g++', *SPEAKER_GATE_FLAGS, '-std=c++17', '-DUSE_ESP32', '-I'+str(stubs),
                               '-I'+str(esphome_root.parent), str(unit), str(ring), '-o', str(binary)],
                              capture_output=True, text=True)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    return binary


@pytest.mark.parametrize('channels', [1, 2])
@pytest.mark.parametrize('scenario', ['reset', 'stop', 'finish', 'error', 'space', 'ready', 'terminal', 'main_task', 'timeout_budget', 'almost_full', 'oversized', 'wrap', 'wait', 'no_space', 'unaligned', 'device_sized_wait', 'wait_timeout'])
def test_pcm_progress_alignment_and_retry(partial_writer, channels, scenario):
    result = subprocess.run(['bash', '-c', 'ulimit -c 0; exec "$@"', 'bash',
                             str(partial_writer), scenario, str(channels)],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
