#include "esp_audio_stack.h"

#if defined(USE_ESP32) && defined(USE_ESP_AUDIO_STACK_HARDWARE_GAIN_CONTROL)

#include <cmath>

namespace esphome::esp_audio_stack {

void ESPAudioStack::configure_hardware_gain_defaults_(float mic, bool has_ref, uint8_t channel, float reference) {
  this->hardware_reference_gain_available_ = has_ref;
  this->hardware_reference_gain_channel_ = channel;
  mic = this->codec_backend_.normalize_input_gain(mic);
  reference = this->codec_backend_.normalize_input_gain(reference);
  this->hardware_gain_requested_[0] = mic;
  this->hardware_gain_applied_[0] = mic;
  this->hardware_gain_requested_[1] = reference;
  this->hardware_gain_applied_[1] = reference;
}

bool ESPAudioStack::request_hardware_gain(bool reference, float db) {
  db = this->codec_backend_.normalize_input_gain(db);
  if (!std::isfinite(db) || (reference && !this->hardware_reference_gain_available_)) return false;
  const uint8_t index = reference ? 1 : 0;
  this->hardware_gain_requested_[index].store(db, std::memory_order_relaxed);
  this->hardware_gain_revision_[index].fetch_add(1, std::memory_order_release);
  this->hardware_gains_dirty_.fetch_or(1U << index, std::memory_order_release);
  // Never create a worker or open hardware just to change an idle setting.
  if (this->audio_task_handle_ != nullptr) xTaskNotifyGive(this->audio_task_handle_);
  return true;
}

void ESPAudioStack::refresh_hardware_gain_readback_(bool diagnostics) {
  if (!this->codec_backend_.is_open() || !this->codec_backend_.hardware_gain_readback_supported()) return;
  const auto readback = this->codec_backend_.read_hardware_gains(diagnostics);
  for (uint8_t index = 0; index < (this->hardware_reference_gain_available_ ? 2 : 1); index++) {
    const float value = index == 0 ? readback.microphone : readback.reference;
    this->hardware_gain_applied_[index].store(value, std::memory_order_relaxed);
    this->hardware_gain_state_[index].store(std::isfinite(value) ? HardwareGainState::GAIN_READ_BACK : HardwareGainState::GAIN_UNKNOWN);
    if (!std::isfinite(value)) this->hardware_gain_failed_[index].store(true);
  }
  this->hardware_gain_result_pending_.store(true, std::memory_order_release);
  this->enable_loop_soon_any_context();
}

void ESPAudioStack::apply_pending_hardware_gains_() {
  const uint8_t dirty = this->hardware_gains_dirty_.exchange(0, std::memory_order_acq_rel);
  bool diagnostics = false;
#ifdef USE_ESP_AUDIO_STACK_GAIN_DIAGNOSTICS
  diagnostics = this->hardware_gain_diagnostics_requested_.exchange(false, std::memory_order_acq_rel);
#endif
  if (dirty == 0 && !diagnostics) return;
  uint32_t revisions[2]{};
  for (uint8_t index = 0; index < 2; index++) {
    if (!(dirty & (1U << index))) continue;
    revisions[index] = this->hardware_gain_revision_[index].load(std::memory_order_acquire);
    const float requested = this->hardware_gain_requested_[index].load(std::memory_order_relaxed);
    const bool ok = index == 0 ? this->codec_backend_.set_input_gain(requested)
                             : this->codec_backend_.set_input_channel_gain(this->hardware_reference_gain_channel_, requested);
    this->hardware_gain_applied_[index].store(ok ? requested : NAN, std::memory_order_relaxed);
    this->hardware_gain_state_[index].store(!ok ? HardwareGainState::GAIN_UNKNOWN :
        (this->codec_backend_.is_open() ? HardwareGainState::GAIN_WRITE_CONFIRMED : HardwareGainState::GAIN_CONFIGURED));
    this->hardware_gain_failed_[index].store(!ok, std::memory_order_relaxed);
    if (!ok) ESP_LOGW("audio_stack.gain", "%s gain write failed; checking registers", index == 0 ? "Microphone" : "Reference");
  }
  // Read every selected microphone and the independent reference, even when a
  // multichannel write failed partway. No previous cache is treated as proof.
  this->refresh_hardware_gain_readback_(diagnostics);
  for (uint8_t index = 0; index < 2; index++)
    if (dirty & (1U << index)) this->hardware_gain_result_revision_[index].store(revisions[index], std::memory_order_release);
#ifdef USE_ESP_AUDIO_STACK_GAIN_DIAGNOSTICS
  if (diagnostics) {
    for (uint8_t index = 0; index < (this->hardware_reference_gain_available_ ? 2 : 1); index++)
      ESP_LOGI("audio_stack.gain", "%s requested=%.1f result=%.1f state=%u (0=configured,1=write,2=readback,3=unknown)",
               index == 0 ? "Microphone" : "Reference", this->hardware_gain_requested_[index].load(),
               this->hardware_gain_applied_[index].load(), static_cast<unsigned>(this->hardware_gain_state_[index].load()));
  }
#endif
  this->hardware_gain_result_pending_.store(true, std::memory_order_release);
  this->enable_loop_soon_any_context();
}

bool ESPAudioStack::hardware_gain_result(bool reference, float *value, bool *persist) const {
  const uint8_t index = reference ? 1 : 0;
  const uint32_t revision = this->hardware_gain_result_revision_[index].load(std::memory_order_acquire);
  if (revision == 0 || revision != this->hardware_gain_revision_[index].load(std::memory_order_acquire)) return false;
  *value = this->hardware_gain_applied_[index].load(std::memory_order_relaxed);
  // Only persist values that the same vendor setter can reproduce on reboot.
  if (persist) *persist = std::isfinite(*value) && this->codec_backend_.normalize_input_gain(*value) == *value;
  return true;
}

#ifdef USE_ESP_AUDIO_STACK_GAIN_DIAGNOSTICS
void ESPAudioStack::request_hardware_gain_diagnostics() {
  this->hardware_gain_diagnostics_requested_.store(true, std::memory_order_release);
  if (this->audio_task_handle_ != nullptr) xTaskNotifyGive(this->audio_task_handle_);
}
#endif

}  // namespace esphome::esp_audio_stack
#endif
