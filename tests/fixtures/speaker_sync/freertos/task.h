#pragma once
#include <chrono>
#include "FreeRTOS.h"
using TaskHandle_t = void *;
inline TaskHandle_t xTaskGetCurrentTaskHandle() { static thread_local char identity; return &identity; }
inline thread_local bool use_speaker_test_clock = false;
inline thread_local TickType_t speaker_test_ticks = 0;
inline TickType_t xTaskGetTickCount() {
  if (use_speaker_test_clock) return speaker_test_ticks;
  using namespace std::chrono;
  return static_cast<TickType_t>(duration_cast<milliseconds>(steady_clock::now().time_since_epoch()).count());
}
