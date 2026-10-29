#pragma once

#include "esphome/core/log.h"
#include <atomic>
#include <cstddef>

namespace esphome::esp_afe {

// Preserve the atomic counter interface while compiling disabled diagnostics
// out of each instance. The tag gives every empty member a distinct type so
// [[no_unique_address]] can overlap them without affecting live state.
template<typename T, size_t Tag, int Initial = 0> class DisabledAfeCounter {
 public:
  constexpr explicit DisabledAfeCounter(T) {}
  T load(std::memory_order = std::memory_order_seq_cst) const { return static_cast<T>(Initial); }
  void store(T, std::memory_order = std::memory_order_seq_cst) {}
  T fetch_add(T, std::memory_order = std::memory_order_seq_cst) { return static_cast<T>(Initial); }
  bool compare_exchange_weak(T &, T, std::memory_order, std::memory_order) { return false; }
};

#if defined(USE_ESP_AUDIO_STACK_TELEMETRY) && ESPHOME_LOG_LEVEL >= ESPHOME_LOG_LEVEL_DEBUG
template<typename T, size_t Tag, int Initial = 0> using AfeDiagnosticCounter = std::atomic<T>;
#else
template<typename T, size_t Tag, int Initial = 0> using AfeDiagnosticCounter = DisabledAfeCounter<T, Tag, Initial>;
#endif

}  // namespace esphome::esp_afe
