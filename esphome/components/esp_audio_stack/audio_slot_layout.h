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

// Effective DMA layout, independent of processor/downmix delivery. Resolve it
// once so the raw tap and the normal RX converter select the same physical mics.
struct AudioRxLayout {
  uint8_t stride{1};
  uint8_t mic_slots[2]{0, 0};
  uint8_t mic_channels{1};
  uint8_t reference_slot{AudioSlotLayout::INVALID};

  static constexpr AudioRxLayout standard(bool stereo, bool mic_right, bool reference, bool reference_right,
                                           uint8_t primary, int8_t secondary) {
    if (reference)
      return {2, {static_cast<uint8_t>(reference_right ? 0 : 1), 0}, 1,
              static_cast<uint8_t>(reference_right ? 1 : 0)};
    if (stereo && secondary >= 0)
      return {2, {primary, static_cast<uint8_t>(secondary)}, 2, AudioSlotLayout::INVALID};
    return {static_cast<uint8_t>(stereo ? 2 : 1), {static_cast<uint8_t>(stereo && mic_right), 0}, 1,
            AudioSlotLayout::INVALID};
  }

  static constexpr AudioRxLayout tdm(AudioSlotLayout slots, uint8_t primary, int8_t secondary,
                                      bool reference, uint8_t reference_channel) {
    return {slots.count(), {slots.index(primary), secondary >= 0 ? slots.index(secondary) : uint8_t{0}},
            static_cast<uint8_t>(secondary >= 0 ? 2 : 1),
            reference ? slots.index(reference_channel) : AudioSlotLayout::INVALID};
  }
};

}  // namespace esphome::esp_audio_stack
