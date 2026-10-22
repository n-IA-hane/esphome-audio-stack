#pragma once

#ifdef USE_ESP_AUDIO_STACK_CAPTURE
#include <cstddef>
#include <cstdint>

namespace esphome::esp_audio_stack {

enum class CapturePoint : uint8_t { RAW_MIC, REFERENCE, PROCESSOR_INPUT, PROCESSOR_OUTPUT, OUTPUT, COUNT };

// A borrowed view, valid only for the duration of the callback. Raw samples
// retain their bus width; all processed surfaces are signed 16-bit PCM.
struct CaptureFrame {
  CapturePoint point;
  const void* data;
  size_t frames;
  uint32_t sample_rate;
  uint32_t generation;
  uint64_t timestamp_us;
  uint8_t bits;
  uint8_t channels;
  uint8_t stride;
  uint8_t slots[2];
};

class AudioCaptureObserver {
 public:
  virtual bool capture_active(CapturePoint point) const = 0;
  virtual void on_capture_frame(const CaptureFrame& frame) = 0;
};

}  // namespace esphome::esp_audio_stack
#endif
