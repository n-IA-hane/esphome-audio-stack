#pragma once
#include <cstdint>

namespace esphome::esp_audio_stack {

// Physical I2S slots selected for DMA, packed in ascending slot order by IDF.
struct AudioSlotLayout {
  uint16_t mask{0};
  static constexpr uint8_t INVALID = 0xff;

  constexpr uint8_t count() const {
    uint16_t remaining = this->mask;
    uint8_t result = 0;
    while (remaining != 0) {
      remaining &= remaining - 1U;
      ++result;
    }
    return result;
  }

  constexpr uint8_t index(uint8_t physical_slot) const {
    if (physical_slot >= 16 || (this->mask & (1U << physical_slot)) == 0) return INVALID;
    return AudioSlotLayout{static_cast<uint16_t>(this->mask & ((1U << physical_slot) - 1U))}.count();
  }
};

}  // namespace esphome::esp_audio_stack
