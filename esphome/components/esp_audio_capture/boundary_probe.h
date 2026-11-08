#pragma once
#include "esphome/core/defines.h"
#if defined(USE_ESP32) && defined(USE_ESP_AUDIO_BOUNDARY_PROBES)
#include <cstddef>
#include <cstdint>

namespace esphome::esp_audio_capture {
// Separate wire points, never indexes into Audio Stack's CapturePoint arrays.
constexpr uint8_t BOUNDARY_FRONTEND = 7;
constexpr uint8_t BOUNDARY_FEED = 8;
struct BoundaryRecord {
  uint64_t position{0}, timestamp_us{0};
  uint32_t session{0}, sequence{0}, instance{0}, duration_us{0};
  uint32_t offered_frames{0}, frames{0}, features{0};
  uint32_t checksum_before{0}, checksum_after{0}, checksum{0};
  int32_t result{0};  // Opaque vendor return: do not assume bytes vs samples.
  uint32_t flags{0}; // 1: PCM changed, 2: invalid consumed count.
  uint8_t point{0}, channels{0};
};
struct BoundaryHealth {
  uint32_t interface_overflow, instance_overflow, unknown_instance, invalid_shape, foreign_frontend;
};
class BoundarySink {
 public:
  // Zero means disabled. A new capture gets a distinct nonzero token.
  virtual uint32_t boundary_session() const = 0;
  virtual void on_boundary(const BoundaryRecord &record, const int16_t *pcm) = 0;
};
// Sink has application lifetime. Registration happens once in component setup.
bool boundary_register_sink(BoundarySink *sink);
BoundaryHealth boundary_health();
}  // namespace esphome::esp_audio_capture
#endif
