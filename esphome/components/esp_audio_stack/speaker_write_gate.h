#pragma once

#ifdef USE_ESP32

#include <atomic>
#include <sys/queue.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <freertos/task.h>

namespace esphome::esp_audio_stack {

enum class SpeakerAdmission : uint8_t { STOPPED, STARTING, READY, DRAINING, STOPPING };

// A producer checks readiness and commits PCM under the same short mutex as reset.
// Only a blocked producer needs a waiter, on its own task stack. As in ESP-IDF's
// pthread condition variable, registration precedes unlock and each waiter has
// a latched semaphore. A fast stop/start or a second producer cannot steal its wake.
class SpeakerWriteGate {
 public:
  bool setup() {
    this->setup_task_ = xTaskGetCurrentTaskHandle();
    this->mutex_ = xSemaphoreCreateMutexStatic(&this->mutex_storage_);
    return this->mutex_ != nullptr;
  }

  template<class TryWrite> void run(TickType_t ticks, TryWrite try_write) {
    if (this->mutex_ == nullptr) return;
    // The main loop itself commits asynchronous speaker readiness.
    if (ticks == portMAX_DELAY && xTaskGetCurrentTaskHandle() == this->setup_task_) ticks = 0;
    const TickType_t start = ticks == 0 || ticks == portMAX_DELAY ? 0 : xTaskGetTickCount();
    if (xSemaphoreTake(this->mutex_, ticks) != pdTRUE) return;
    if (!try_write() && ticks != 0)
      this->wait_(ticks, start, try_write);
    xSemaphoreGive(this->mutex_);
  }

  template<class Reset> void reset(Reset reset) {
    if (this->mutex_ == nullptr) {
      reset();
      return;
    }
    xSemaphoreTake(this->mutex_, portMAX_DELAY);
    reset();
    this->notify_locked_(true);
    xSemaphoreGive(this->mutex_);
  }

  void notify(bool cancel = false) {
    // Both publication and registration use an acquire/release RMW. When there
    // is no waiter, the next registration acquires this publication before it
    // rechecks readiness/space. A separate load could miss that interleaving.
    if (this->waiters_count_.fetch_add(0, std::memory_order_acq_rel) == 0) return;
    xSemaphoreTake(this->mutex_, portMAX_DELAY);
    this->notify_locked_(cancel);
    xSemaphoreGive(this->mutex_);
  }

 private:
  struct Waiter {
    SemaphoreHandle_t signal;
    bool cancelled{false};
    LIST_ENTRY(Waiter) link;
  };
  LIST_HEAD(Waiters, Waiter) waiters_{};
  StaticSemaphore_t mutex_storage_;
  TaskHandle_t setup_task_{nullptr};
  SemaphoreHandle_t mutex_{nullptr};
  std::atomic<uint32_t> waiters_count_{0};

  void notify_locked_(bool cancel) {
    Waiter *waiter;
    LIST_FOREACH(waiter, &this->waiters_, link) {
      if (cancel) waiter->cancelled = true;
      xSemaphoreGive(waiter->signal);
    }
  }

  template<class TryWrite> __attribute__((noinline)) void wait_(TickType_t ticks, TickType_t start,
                                                                TryWrite &try_write) {
    StaticSemaphore_t storage;
    Waiter waiter{};
    waiter.signal = xSemaphoreCreateBinaryStatic(&storage);
    if (waiter.signal == nullptr) return;
    LIST_INSERT_HEAD(&this->waiters_, &waiter, link);
    this->waiters_count_.fetch_add(1, std::memory_order_acq_rel);
    // Space or readiness may have changed before notify() observed a waiter.
    while (!waiter.cancelled && !try_write()) {
      TickType_t remaining = ticks;
      if (ticks != portMAX_DELAY) {
        const TickType_t elapsed = xTaskGetTickCount() - start;
        if (elapsed >= ticks) break;
        remaining -= elapsed;
      }
      xSemaphoreGive(this->mutex_);
      const bool signalled = xSemaphoreTake(waiter.signal, remaining) == pdTRUE;
      // Reacquire even on timeout to remove our stack node before returning.
      xSemaphoreTake(this->mutex_, portMAX_DELAY);
      if (!signalled) break;
    }
    LIST_REMOVE(&waiter, link);
    this->waiters_count_.fetch_sub(1, std::memory_order_acq_rel);
    vSemaphoreDelete(waiter.signal);
  }
};

}  // namespace esphome::esp_audio_stack

#endif
