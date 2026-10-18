"""Compile the actual adapter against both ESPHome handle representations.

The small base/SDK doubles model layout and ownership, not audio or scheduling.
Full real-ESP-IDF and hardware tests remain separate evidence.
"""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = "esp_audio_stack"
NAMESPACE = "esp_audio_stack"


@pytest.mark.parametrize("accessor", [False, True], ids=["esphome_2026_9_1", "upstream_accessor"])
def test_ring_buffer_handle_ownership(tmp_path: Path, accessor: bool) -> None:
    compiler = shutil.which("g++")
    assert compiler is not None, "C++ runtime tests require g++"
    include = tmp_path / "include"
    files = {
        "esp_heap_caps.h": r"""
#pragma once
#include <cassert>
#include <cstdlib>
#include <cstddef>
#include <cstdint>
inline constexpr unsigned MALLOC_CAP_8BIT=1, MALLOC_CAP_INTERNAL=2, MALLOC_CAP_SPIRAM=4;
inline int allocated=0, freed=0, deleted=0;
inline bool fail_allocate=false, fail_create=false;
inline void *heap_caps_malloc(size_t n, unsigned) {
  if (fail_allocate) { fail_allocate=false; return nullptr; }
  ++allocated; return std::malloc(n);
}
inline void heap_caps_free(void *p) { if (p) {++freed; std::free(p);} }
""",
        "esp_memory_utils.h": "#pragma once\ninline bool esp_ptr_internal(const void *) { return true; }\n",
        "freertos/FreeRTOS.h": r"""
#pragma once
#include <cstddef>
#include <cstdint>
using TickType_t=uint32_t; using UBaseType_t=unsigned; using BaseType_t=int;
inline constexpr int pdPASS=1;
""",
        "freertos/ringbuf.h": r"""
#pragma once
#include "FreeRTOS.h"
#include "esp_heap_caps.h"
using RingbufHandle_t=void *;
enum RingbufferType_t {RINGBUF_TYPE_BYTEBUF,RINGBUF_TYPE_NOSPLIT};
struct StaticRingbuffer_t {bool live=false;};
inline RingbufHandle_t xRingbufferCreateStatic(size_t, RingbufferType_t, uint8_t *, StaticRingbuffer_t *s) {
  if (fail_create) {fail_create=false;return nullptr;}
  s->live=true;return s;
}
inline void vRingbufferDelete(RingbufHandle_t h) {
  assert(h && static_cast<StaticRingbuffer_t *>(h)->live);
  static_cast<StaticRingbuffer_t *>(h)->live=false;++deleted;
}
inline void *xRingbufferReceive(RingbufHandle_t,size_t *,TickType_t) {return nullptr;}
inline void vRingbufferReturnItem(RingbufHandle_t,void *) {}
inline bool xRingbufferSend(RingbufHandle_t,const void *,size_t,TickType_t) {return false;}
inline void vRingbufferGetInfo(RingbufHandle_t,void *,void *,void *,void *,unsigned *) {}
""",
        "esphome/core/log.h": r"""
#pragma once
#define ESP_LOGE(...) do {} while (0)
#define ESP_LOGI(...) do {} while (0)
""",
    }
    handle = (
        "RingbufHandle_t handle_() const { return const_cast<StaticRingbuffer_t *>(&structure_); }"
        if accessor else "RingbufHandle_t handle_{nullptr};"
    )
    predicate = "storage_ != nullptr" if accessor else "handle_ != nullptr"
    expression = "handle_()" if accessor else "handle_"
    files["esphome/components/ring_buffer/ring_buffer.h"] = r"""
#pragma once
#include <memory>
#include "freertos/ringbuf.h"
namespace esphome::ring_buffer {
class RingBuffer {
 public:
  ~RingBuffer() { if (PREDICATE) {vRingbufferDelete(EXPRESSION);heap_caps_free(storage_);} }
  size_t read(void *,size_t,TickType_t=0) {return 0;}
  size_t write(const void *,size_t) {return 0;}
  size_t write_without_replacement(const void *,size_t,TickType_t=0,bool=true) {return 0;}
  size_t free() const {return size_;}
  BaseType_t reset() {return pdPASS;}
 protected:
  bool discard_bytes_(size_t) {return false;}
  HANDLE
  StaticRingbuffer_t structure_;
  uint8_t *storage_{nullptr};
  size_t size_{0};
};
}
""".replace("PREDICATE", predicate).replace("EXPRESSION", expression).replace("HANDLE", handle)
    for name, contents in files.items():
        path = include / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
    program = tmp_path / "main.cpp"
    program.write_text(r"""
#include "audio_core_ring_buffer_caps.h"
namespace esphome::esp_audio_stack {
void log_config(const char *,const char *,...) {}
void log_info(const char *,const char *,...) {}
void log_warn(const char *,const char *,...) {}
void log_error(const char *,const char *,...) {}
}
using esphome::NAMESPACE_NAME::CapsRingBuffer;
int main() {
  {CapsRingBuffer empty;}
  assert(allocated==0 && freed==0 && deleted==0);
  {
    CapsRingBuffer rb;
    assert(rb.install(32,MALLOC_CAP_8BIT));
    assert(rb.probe_storage()!=nullptr);
  }
  assert(allocated==1 && freed==1 && deleted==1);
  {
    CapsRingBuffer rb;fail_allocate=true;
    assert(!rb.install(32,MALLOC_CAP_8BIT));
    assert(rb.probe_storage()==nullptr);
  }
  assert(allocated==1 && freed==1 && deleted==1);
  {
    CapsRingBuffer rb;fail_create=true;
    assert(!rb.install(32,MALLOC_CAP_8BIT));
    assert(rb.probe_storage()==nullptr);
  }
  assert(allocated==2 && freed==2 && deleted==1);
  for (int i=0;i<100;++i) {
    auto rb=std::make_unique<CapsRingBuffer>();
    assert(rb->install(32,MALLOC_CAP_8BIT));
  }
  assert(allocated==102 && freed==102 && deleted==101);
}
""".replace("NAMESPACE_NAME", NAMESPACE))
    executable = tmp_path / "contract"
    subprocess.run(
        [compiler, "-std=c++20", "-DUSE_ESP32", "-I", str(include),
         "-I", str(ROOT / "esphome/components" / COMPONENT),
         str(program), "-o", str(executable)],
        check=True, capture_output=True, text=True,
    )
    subprocess.run([str(executable)], check=True, capture_output=True, text=True, timeout=10)
