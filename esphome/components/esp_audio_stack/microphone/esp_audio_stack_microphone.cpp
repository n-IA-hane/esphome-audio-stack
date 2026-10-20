#include "esp_audio_stack_microphone.h"

#ifdef USE_ESP32

#include <cinttypes>
#include <cstring>

#include "esphome/core/log.h"
#include "../audio_core_log_utils.h"

namespace esphome::esp_audio_stack {

static const char *const TAG = "audio_stack.mic";

void ESPAudioStackMicrophone::setup() {
  ESP_LOGCONFIG(TAG, "Setting up ESP Audio Stack Microphone...");

  this->parent_->add_on_lifecycle_callback([this]() { this->enable_loop_soon_any_context(); });

  // Configure audio stream info for 16-bit mono PCM at output rate
  // When rate conversion is active, mic delivers data at output_sample_rate (e.g. 16 kHz).
  // AudioStreamInfo constructor: (bits_per_sample, channels, sample_rate)
  this->audio_stream_info_ = audio::AudioStreamInfo(16, 1, this->parent_->get_output_sample_rate());

  // Pre-allocate callback buffer to avoid heap allocation in the RT audio task.
  // Do not grow this vector in on_audio_data_(): that callback runs in the
  // parent audio task.
  const size_t callback_buffer_bytes = this->parent_->get_mic_callback_buffer_size();
  this->audio_buffer_.reserve(callback_buffer_bytes);
  ESP_LOGCONFIG(TAG, "  Callback Buffer: %u bytes", (unsigned) callback_buffer_bytes);

  // Standard microphone output is always post-processor. MWW, VA and
  // call components all consume the same cleaned stream.
  if (!this->parent_->add_mic_data_callback(ESPAudioStackMicrophone::mic_data_callback, this)) {
    ESP_LOGE(TAG, "Failed to register parent mic callback");
    this->mark_failed();
  }
}

void ESPAudioStackMicrophone::dump_config() {
  ESP_LOGCONFIG(TAG, "ESP Audio Stack Microphone:");
  ESP_LOGCONFIG(TAG, "  Sample Rate: %" PRIu32 " Hz", this->parent_->get_output_sample_rate());
  ESP_LOGCONFIG(TAG, "  Bits Per Sample: 16");
  ESP_LOGCONFIG(TAG, "  Channels: 1 (mono)");
}

void ESPAudioStackMicrophone::start() {
  if (this->is_failed())
    return;
  uint32_t count = this->active_listeners_.load(std::memory_order_relaxed);
  while (count < MAX_LISTENERS) {
    if (this->active_listeners_.compare_exchange_weak(count, count + 1, std::memory_order_acq_rel,
                                                    std::memory_order_relaxed)) {
      this->enable_loop_soon_any_context();
      return;
    }
  }
  ESP_LOGW(TAG, "No free listener slots");
}

bool ESPAudioStackMicrophone::release_listener_() {
  uint32_t count = this->active_listeners_.load(std::memory_order_relaxed);
  while (count != 0) {
    if (this->active_listeners_.compare_exchange_weak(count, count - 1, std::memory_order_acq_rel,
                                                    std::memory_order_relaxed))
      return count == 1;
  }
  return false;
}

void ESPAudioStackMicrophone::stop() {
  if (this->is_failed() || !this->release_listener_())
    return;
  // Only the final consumer requests a stop; the main loop owns hardware.
  this->enable_loop_soon_any_context();
}

void ESPAudioStackMicrophone::mic_data_callback(void *ctx, const uint8_t *data, size_t len) {
  static_cast<ESPAudioStackMicrophone *>(ctx)->on_audio_data_(data, len);
}

void ESPAudioStackMicrophone::on_audio_data_(const uint8_t *data, size_t len) {
  if (this->state_ != microphone::STATE_RUNNING) {
    return;
  }

  if (len > this->audio_buffer_.capacity()) {
    LOG_W_THROTTLED("Mic callback frame too large: %u > %u bytes; dropping", (unsigned) len,
                    (unsigned) this->audio_buffer_.capacity());
    return;
  }
  this->audio_buffer_.resize(len);
  const bool muted = this->mute_state_;
  if (muted) {
    std::memset(this->audio_buffer_.data(), 0, len);
  } else {
    std::memcpy(this->audio_buffer_.data(), data, len);
  }
  // ESPHome's base Microphone wrapper allocates a temporary zero vector when
  // mute_state_ is true. We have already zero-filled the preallocated callback
  // buffer, so clear the flag only while dispatching to avoid RT-task heap churn.
  if (muted)
    this->mute_state_ = false;
  this->data_callbacks_.call(this->audio_buffer_);
  if (muted)
    this->mute_state_ = true;
}

void ESPAudioStackMicrophone::loop() {
  // Propagate I2S errors from parent audio task
  if (this->parent_->has_i2s_error()) {
    if (!this->i2s_error_latched_) {
      ESP_LOGE(TAG, "I2S error detected in audio task");
      this->status_set_error(LOG_STR("I2S read error in audio task"));
      this->i2s_error_latched_ = true;
    }
  } else if (this->i2s_error_latched_) {
    this->status_clear_error();
    this->i2s_error_latched_ = false;
    ESP_LOGI(TAG, "I2S audio path recovered");
  }

  const uint32_t count = this->active_listeners_.load(std::memory_order_acquire);

  // Start the microphone when at least one consumer requested capture.
  if ((count > 0) && (this->state_ == microphone::STATE_STOPPED)) {
    this->state_ = microphone::STATE_STARTING;
  }

  // Stop the microphone after the final consumer releases capture.
  if ((count == 0) && (this->state_ == microphone::STATE_RUNNING)) {
    this->state_ = microphone::STATE_STOPPING;
  }

  switch (this->state_) {
    case microphone::STATE_STARTING:
      if (this->status_has_error() && !this->i2s_error_latched_) {
        break;
      }
      if (this->active_listeners_.load(std::memory_order_acquire) == 0) {
        if (this->parent_registered_) this->parent_->unregister_mic_consumer(this);
        this->parent_registered_ = false;
        this->state_ = microphone::STATE_STOPPED;
        break;
      }
      ESP_LOGI(TAG, "Microphone started");
      if (!this->parent_registered_ && !this->parent_->register_mic_consumer(this)) {
        ESP_LOGW(TAG, "Parent audio stack refused mic consumer registration");
        this->release_listener_();
        this->state_ = microphone::STATE_STOPPED;
        break;
      }
      this->parent_registered_ = true;
      if (this->parent_->is_starting()) {
        this->disable_loop();
        break;
      }
      if (!this->parent_->is_running()) {
        ESP_LOGW(TAG, "Parent audio stack failed to start; aborting microphone start");
        this->parent_->unregister_mic_consumer(this);
        this->parent_registered_ = false;
        this->release_listener_();
        this->state_ = microphone::STATE_STOPPED;
        break;
      }
      this->state_ = microphone::STATE_RUNNING;
      break;

    case microphone::STATE_RUNNING:
      break;

    case microphone::STATE_STOPPING:
      ESP_LOGI(TAG, "Microphone stopped");
      this->parent_->unregister_mic_consumer(this);
      this->parent_registered_ = false;
      this->state_ = microphone::STATE_STOPPED;
      break;

    case microphone::STATE_STOPPED:
      this->disable_loop();
      break;
  }
}

}  // namespace esphome::esp_audio_stack

#endif  // USE_ESP32
