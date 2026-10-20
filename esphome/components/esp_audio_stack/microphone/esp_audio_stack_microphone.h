#pragma once

#ifdef USE_ESP32

#include "esphome/core/component.h"
#include "esphome/core/helpers.h"
#include "esphome/components/microphone/microphone.h"
#include "../esp_audio_stack.h"

#include <atomic>
#include <vector>

#include <freertos/FreeRTOS.h>

namespace esphome::esp_audio_stack {

class ESPAudioStackMicrophone final : public microphone::Microphone, public Component, public Parented<ESPAudioStack> {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::DATA; }

  // microphone::Microphone interface
  void start() override;
  void stop() override;

 protected:
  static void mic_data_callback(void *ctx, const uint8_t *data, size_t len);
  void on_audio_data_(const uint8_t *data, size_t len);

  std::vector<uint8_t> audio_buffer_;

  // Reference counting for multiple listeners (voice_assistant, wake_word, call components, etc.)
  // Valid before setup(), so early capture/stop automations remain balanced.
  std::atomic<uint32_t> active_listeners_{0};
  bool release_listener_();
  bool parent_registered_{false};
  bool i2s_error_latched_{false};
};

}  // namespace esphome::esp_audio_stack

#endif  // USE_ESP32
