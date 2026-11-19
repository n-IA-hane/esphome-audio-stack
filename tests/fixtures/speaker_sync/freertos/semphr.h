#pragma once
#include "task.h"
#include <condition_variable>
#include <functional>
#include <mutex>

struct StaticSemaphore_t {
  std::mutex mutex;
  std::condition_variable changed;
  bool available{false};
  bool is_gate{false};
};
using SemaphoreHandle_t = StaticSemaphore_t *;
inline thread_local bool simulate_speaker_timeout = false;
inline thread_local std::function<void()> speaker_parking_hook;
inline thread_local std::function<void(TickType_t)> speaker_wait_hook;
inline SemaphoreHandle_t xSemaphoreCreateMutexStatic(StaticSemaphore_t *s) {
  s->available = s->is_gate = true;
  return s;
}
inline SemaphoreHandle_t xSemaphoreCreateBinaryStatic(StaticSemaphore_t *s) { return s; }
inline void vSemaphoreDelete(SemaphoreHandle_t) {}
inline int xSemaphoreTake(SemaphoreHandle_t s, TickType_t ticks) {
  std::unique_lock lock(s->mutex);
  if (!s->available && ticks != 0) {
    if (!s->is_gate && speaker_wait_hook) {
      lock.unlock();
      speaker_wait_hook(ticks);
      lock.lock();
    }
    if (!s->is_gate && !s->available && simulate_speaker_timeout && ticks != portMAX_DELAY) return 0;
    if (!s->is_gate && !s->available && speaker_parking_hook) speaker_parking_hook();
    if (ticks == portMAX_DELAY)
      s->changed.wait(lock, [s] { return s->available; });
    else
      s->changed.wait_for(lock, std::chrono::milliseconds(ticks), [s] { return s->available; });
  }
  if (!s->available) return 0;
  s->available = false;
  return 1;
}
inline int xSemaphoreGive(SemaphoreHandle_t s) {
  std::lock_guard lock(s->mutex);
  s->available = true;
  s->changed.notify_one();
  return 1;
}
