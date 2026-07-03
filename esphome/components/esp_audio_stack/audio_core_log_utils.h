#pragma once

#ifdef USE_ESP32

#include <cstdint>

#include "esphome/core/log.h"

// Rate-limited WARN for hot audio paths: emit on the first 5 occurrences and
// then every 100th. Each expansion site owns its static counter.
//
// The macro expects a `TAG` symbol in scope, matching ESPHome ESP_LOG*
// conventions used by esp_audio_stack .cpp files.
#define LOG_W_THROTTLED(fmt, ...) \
  do { \
    static uint32_t _throttled_n = 0; \
    _throttled_n++; \
    if (_throttled_n <= 5 || _throttled_n % 100 == 0) { \
      ESP_LOGW(TAG, fmt " [n=%u]", ##__VA_ARGS__, (unsigned) _throttled_n); \
    } \
  } while (0)

#endif  // USE_ESP32
