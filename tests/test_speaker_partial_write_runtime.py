"""Exercise production play() with ESPHome RingBuffer and bounded BYTEBUF I/O.

Only the FreeRTOS storage/wait adapter is simulated. The actual RingBuffer
implementation performs writes, reads and wrap handling. This is a PCM contract
check, not a FreeRTOS scheduling or physical audio qualification.
"""
from pathlib import Path
import subprocess

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
struct StaticRingbuffer_t {};
inline RingbufHandle_t xRingbufferCreateStatic(size_t size, int, uint8_t *storage, StaticRingbuffer_t *) {
  return new FakeRing{storage,size};
}
inline void vRingbufferDelete(RingbufHandle_t ring) { delete ring; }
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
        'esphome/core/log.h': '#pragma once\n#define ESP_LOGD(...) ((void)0)\n',
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
    resampler_source = (ROOT / 'esphome/components/resampler/speaker/resampler_speaker.cpp').read_text()
    start = resampler_source.index('size_t ResamplerSpeaker::play(')
    end, depth = resampler_source.index('{', start) + 1, 1
    while depth:
        depth += (resampler_source[end] == '{') - (resampler_source[end] == '}')
        end += 1
    resampler_body = resampler_source[start:end]
    harness = r'''
#include <atomic>
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
  unsigned get_speaker_channels() const { return channels; }
  size_t play(const uint8_t*,size_t,TickType_t);
};
namespace ring_buffer = esphome::ring_buffer;
struct FakeInfo {
  unsigned channels;
  size_t frames_to_bytes(size_t frames) const { return frames * channels * sizeof(int16_t); }
};
struct FakeSpeaker {
  bool is_running() const { return true; }
  size_t play(const uint8_t *,size_t,TickType_t) { assert(false); return 0; }
};
struct ResamplerSpeaker {
  std::weak_ptr<RingBuffer> ring_buffer_;
  FakeSpeaker sink;
  FakeSpeaker *output_speaker_{&sink};
  FakeInfo audio_stream_info_;
  bool is_stopped() const { return false; }
  void start() { assert(false); }
  bool requires_resampling_() const { return true; }
  size_t play(const uint8_t *,size_t,TickType_t);
};
// PRODUCTION_PLAY
// PRODUCTION_RESAMPLER_PLAY
std::vector<uint8_t> sequence(size_t size,unsigned seed=0) {
  std::vector<uint8_t> result(size);
  for(size_t i=0;i<size;++i) result[i]=(i*37+seed)%251;
  return result;
}
std::vector<uint8_t> drain(RingBuffer &ring,size_t limit) {
  std::vector<uint8_t> result(limit);
  result.resize(ring.read(result.data(),result.size(),0)); return result;
}
int main(int argc,char **argv) {
  assert(argc==3 || argc==4); std::string scenario=argv[1]; unsigned channels=std::atoi(argv[2]);
  const size_t frame=channels*sizeof(int16_t);
  const size_t capacity=scenario=="device_sized_wait" ? 3200*channels : 5*frame+(scenario=="wrap" ? 1 : 0);
  std::shared_ptr<RingBuffer> ring=RingBuffer::create(capacity);
  ESPAudioStack stack{ring.get(),false,channels};
  ResamplerSpeaker resampler;
  resampler.ring_buffer_=ring; resampler.audio_stream_info_={channels};
  auto play=[&](const uint8_t *data,size_t length,TickType_t ticks) {
    return argc==4 ? resampler.play(data,length,ticks) : stack.play(data,length,ticks);
  };
  auto input=sequence(17*frame+1,101);
  if(scenario=="oversized") {
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
    if(scenario!="wait_timeout") wait_hook=[&]{consumed=drain(*ring,frame);};
    if(scenario=="device_sized_wait") input=sequence(8192);
    size_t written=play(input.data(),input.size(),scenario=="no_space" ? 0 : 7);
    if(scenario=="wait" || scenario=="device_sized_wait") {
      const size_t expected=argc==4 ? 1 : frame;
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
  } else if(scenario=="fragments") {
    // Resampler input is a byte stream. RingBufferAudioSource owns stitching
    // PCM frames across chunks; the writer must retain every byte unchanged.
    assert(play(input.data(),1,0)==1);
    assert(play(input.data()+1,2*frame-1,0)==2*frame-1);
    assert(drain(*ring,capacity)==std::vector<uint8_t>(input.begin(),input.begin()+2*frame));
  } else { assert(false); }
  assert(ring->available()==0);
}
'''.replace('// PRODUCTION_PLAY', body).replace('// PRODUCTION_RESAMPLER_PLAY', resampler_body)
    unit = directory / 'partial.cpp'
    unit.write_text(harness)
    binary = directory / 'partial'
    esphome_root = Path(esphome.__file__).resolve().parent
    ring = esphome_root / 'components/ring_buffer/ring_buffer.cpp'
    subprocess.run(['g++', '-std=c++17', '-DUSE_ESP32', '-I'+str(stubs),
                    '-I'+str(esphome_root.parent), str(unit), str(ring), '-o', str(binary)],
                   check=True, capture_output=True, text=True)
    return binary


@pytest.mark.parametrize('channels', [1, 2])
@pytest.mark.parametrize('scenario', ['almost_full', 'oversized', 'wrap', 'wait', 'no_space', 'unaligned'])
def test_pcm_progress_alignment_and_retry(partial_writer, channels, scenario):
    result = subprocess.run(['bash', '-c', 'ulimit -c 0; exec "$@"', 'bash',
                             str(partial_writer), scenario, str(channels)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('channels', [1, 2])
@pytest.mark.parametrize('scenario', ['oversized', 'wait', 'no_space', 'device_sized_wait', 'wait_timeout', 'fragments'])
def test_resampler_bounded_byte_stream_and_retry(partial_writer, channels, scenario):
    result = subprocess.run(['bash', '-c', 'ulimit -c 0; exec "$@"', 'bash',
                             str(partial_writer), scenario, str(channels), 'resampler'],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
