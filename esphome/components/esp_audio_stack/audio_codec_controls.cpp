#include "esp_audio_stack.h"

#if defined(USE_ESP32) && defined(USE_ESP_AUDIO_STACK_HARDWARE_GAIN_CONTROL)

#include <cmath>

namespace esphome::esp_audio_stack {

void ESPAudioStack::configure_hardware_gain_defaults_(float mic, bool has_ref, uint8_t channel, float reference) {
  this->hardware_reference_gain_available_ = has_ref;
  this->hardware_reference_gain_channel_ = channel;
  this->hardware_gain_requested_[0] = mic;
  this->hardware_gain_applied_[0] = mic;
  this->hardware_gain_requested_[1] = reference;
  this->hardware_gain_applied_[1] = reference;
}

bool ESPAudioStack::request_hardware_gain(bool reference, float db) {
  if (!std::isfinite(db) || db < 0 || db > 37.5f || (reference && !this->hardware_reference_gain_available_))
    return false;
  const uint8_t index = reference ? 1 : 0;
  this->hardware_gain_requested_[index].store(db, std::memory_order_relaxed);
  this->hardware_gain_revision_[index].fetch_add(1, std::memory_order_release);
  this->hardware_gains_dirty_.fetch_or(1U << index, std::memory_order_release);
  // Never create a worker or open hardware just to change an idle setting.
  if (this->audio_task_handle_ != nullptr) xTaskNotifyGive(this->audio_task_handle_);
  return true;
}

void ESPAudioStack::apply_pending_hardware_gains_() {
  const uint8_t dirty = this->hardware_gains_dirty_.exchange(0, std::memory_order_acq_rel);
  if (dirty == 0) return;
  for (uint8_t index = 0; index < 2; index++) {
    if (!(dirty & (1U << index))) continue;
    const uint32_t revision = this->hardware_gain_revision_[index].load(std::memory_order_acquire);
    const float requested = this->hardware_gain_requested_[index].load(std::memory_order_relaxed);
    const bool ok =
        index == 0 ? this->codec_backend_.set_input_gain(requested)
                   : this->codec_backend_.set_input_channel_gain(this->hardware_reference_gain_channel_, requested);
    if (ok)
      this->hardware_gain_applied_[index].store(requested, std::memory_order_relaxed);
    else
      ESP_LOGW("audio_stack.gain", "%s PGA write failed; last accepted setting %.1f dB",
               index == 0 ? "Microphone" : "Reference", this->hardware_gain_applied_[index].load());
    this->hardware_gain_failed_[index].store(!ok, std::memory_order_relaxed);
    this->hardware_gain_result_revision_[index].store(revision, std::memory_order_release);
  }
  this->hardware_gain_result_pending_.store(true, std::memory_order_release);
  this->enable_loop_soon_any_context();
}

bool ESPAudioStack::hardware_gain_result(bool reference, float* value) const {
  const uint8_t index = reference ? 1 : 0;
  const uint32_t revision = this->hardware_gain_result_revision_[index].load(std::memory_order_acquire);
  if (revision == 0 || revision != this->hardware_gain_revision_[index].load(std::memory_order_acquire)) return false;
  *value = this->hardware_gain_applied_[index].load(std::memory_order_relaxed);
  return true;
}

}  // namespace esphome::esp_audio_stack
#endif
