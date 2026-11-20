#pragma once

#include "esp_afe.h"
#include "esphome/core/log.h"

#if defined(USE_ESP32) && !defined(CLANG_TIDY)
namespace esphome::esp_afe {

#if defined(USE_ESP_AUDIO_STACK_TELEMETRY) && ESPHOME_LOG_LEVEL >= ESPHOME_LOG_LEVEL_DEBUG
static constexpr bool ESP_AFE_TIMING_TELEMETRY = true;
#else
static constexpr bool ESP_AFE_TIMING_TELEMETRY = false;
#endif

#if defined(USE_ESP_AUDIO_STACK_TELEMETRY) && ESPHOME_LOG_LEVEL >= ESPHOME_LOG_LEVEL_DEBUG
static constexpr bool ESP_AFE_DIAGNOSTIC_COUNTERS = true;
#else
static constexpr bool ESP_AFE_DIAGNOSTIC_COUNTERS = false;
#endif

template<typename Counter> static inline void diag_add(Counter &counter, uint32_t value = 1) {
  if constexpr (ESP_AFE_DIAGNOSTIC_COUNTERS) {
    counter.fetch_add(value, std::memory_order_relaxed);
  } else {
    (void) counter;
    (void) value;
  }
}

template<typename Counter> static inline uint32_t diag_increment_and_get(Counter &counter) {
  if constexpr (ESP_AFE_DIAGNOSTIC_COUNTERS) {
    return counter.fetch_add(1, std::memory_order_relaxed) + 1;
  } else {
    (void) counter;
    return 0;
  }
}

template<typename Counter> static inline void update_peak_atomic(Counter &peak, uint32_t value) {
  if constexpr (ESP_AFE_DIAGNOSTIC_COUNTERS) {
    uint32_t current = peak.load(std::memory_order_relaxed);
    while (value > current &&
           !peak.compare_exchange_weak(current, value, std::memory_order_relaxed, std::memory_order_relaxed)) {
    }
  } else {
    (void) peak;
    (void) value;
  }
}

template<typename Counter> static inline void decrement_if_nonzero(Counter &counter) {
  if constexpr (ESP_AFE_DIAGNOSTIC_COUNTERS) {
    uint32_t current = counter.load(std::memory_order_relaxed);
    while (current > 0 &&
           !counter.compare_exchange_weak(current, current - 1, std::memory_order_relaxed, std::memory_order_relaxed)) {
    }
  } else {
    (void) counter;
  }
}

}  // namespace esphome::esp_afe
#endif
