#include "esp_audio_stack_speaker.h"

#ifdef USE_ESP32

#include <cinttypes>
#include <cmath>

#include "esphome/core/hal.h"
#include "esphome/core/log.h"

namespace esphome::esp_audio_stack {

static const char *const TAG = "audio_stack.spk";

void ESPAudioStackSpeaker::setup() {
  ESP_LOGCONFIG(TAG, "Setting up ESP Audio Stack Speaker...");


  this->audio_stream_info_ =
      audio::AudioStreamInfo(16, this->parent_->get_speaker_channels(), this->parent_->get_sample_rate());

  this->parent_->add_on_lifecycle_callback([this]() { this->enable_loop_soon_any_context(); });

  // Forward frame-played notifications from I2S audio task to mixer callbacks.
  // Without this, mixer source speakers can't track pending_playback_frames.
  if (!this->parent_->add_speaker_output_callback(ESPAudioStackSpeaker::speaker_output_callback, this)) {
    ESP_LOGE(TAG, "Failed to register parent speaker callback");
    this->mark_failed();
  }
}

void ESPAudioStackSpeaker::dump_config() {
  ESP_LOGCONFIG(TAG, "ESP Audio Stack Speaker:");
  ESP_LOGCONFIG(TAG, "  Sample Rate: %" PRIu32 " Hz", this->parent_->get_sample_rate());
  ESP_LOGCONFIG(TAG, "  Bits Per Sample: 16");
  ESP_LOGCONFIG(TAG, "  Channels: %u", (unsigned) this->parent_->get_speaker_channels());
  ESP_LOGCONFIG(TAG, "  Buffer Size: %u bytes", (unsigned) this->parent_->get_speaker_buffer_size());
  if (this->timeout_.has_value()) {
    ESP_LOGCONFIG(TAG, "  Timeout: %u ms", (unsigned) this->timeout_.value());
  } else {
    ESP_LOGCONFIG(TAG, "  Timeout: never");
  }
}

void ESPAudioStackSpeaker::start() {
  if (this->is_failed())
    return;

  // Clear finishing flag on new stream start
  this->finishing_ = false;
  if (this->pause_state_) {
    this->set_pause_state(false);
  }
  this->last_write_ms_ = millis();

  // Idempotent: register listener only once per stream session.
  bool expected = false;
  if (!this->listener_registered_.compare_exchange_strong(expected, true, std::memory_order_acq_rel,
                                                          std::memory_order_relaxed))
    return;

  this->enable_loop_soon_any_context();
}

void ESPAudioStackSpeaker::stop() {
  if (this->is_failed())
    return;

  this->finishing_ = false;
  if (this->pause_state_) {
    this->set_pause_state(false);
  }
  if (!this->listener_registered_.exchange(false, std::memory_order_acq_rel))
    return;

  this->enable_loop_soon_any_context();
}

void ESPAudioStackSpeaker::finish() {
  // Non-blocking: set flag, loop() will call stop() once buffer is drained
  if (this->pause_state_) {
    this->set_pause_state(false);
  }
  this->finishing_ = true;
  this->enable_loop_soon_any_context();
}

void ESPAudioStackSpeaker::speaker_output_callback(void *ctx, uint32_t frames, int64_t timestamp) {
  static_cast<ESPAudioStackSpeaker *>(ctx)->audio_output_callback_.call(frames, timestamp);
}

size_t ESPAudioStackSpeaker::play(const uint8_t *data, size_t length) { return this->play(data, length, 0); }

size_t ESPAudioStackSpeaker::play(const uint8_t *data, size_t length, TickType_t ticks_to_wait) {
  if (this->is_failed()) {
    return 0;
  }

  if (this->state_ != speaker::STATE_RUNNING && this->state_ != speaker::STATE_STARTING) {
    this->start();
  }

  if (!this->listener_registered_.load(std::memory_order_acquire)) {
    // Honor the producer's backpressure budget, as the native I2S speaker does.
    // A zero budget remains nonblocking; retry scheduling belongs to that caller.
    vTaskDelay(ticks_to_wait);
    return 0;
  }

  size_t written = this->parent_->play(data, length, ticks_to_wait);
  if (written > 0) {
    this->last_write_ms_ = millis();
  }
  return written;
}

bool ESPAudioStackSpeaker::has_buffered_data() const {
  return this->parent_->get_speaker_buffer_available() > 0 || this->parent_->has_pending_speaker_output();
}

void ESPAudioStackSpeaker::set_volume(float volume) {
  if (!std::isfinite(volume) || volume < 0.0f) {
    volume = 0.0f;
  } else if (volume > 1.0f) {
    volume = 1.0f;
  }
  this->volume_ = volume;

#ifdef USE_AUDIO_DAC
  if (this->audio_dac_ != nullptr) {
    ESP_LOGD(TAG, "Speaker set_volume: %.3f -> audio_dac", volume);
    if (!this->mute_state_ && volume > 0.0f) {
      this->audio_dac_->set_mute_off();
    }
    this->audio_dac_->set_volume(volume);
    if (this->mute_state_) {
      this->audio_dac_->set_mute_on();
    }
  } else
#endif
  {
    ESP_LOGD(TAG, "Speaker set_volume: %.3f -> output volume layer", volume);
    if (this->mute_state_) {
      this->parent_->set_output_volume_q31(0);
    } else {
      this->parent_->set_output_volume(volume);
    }
  }
}

void ESPAudioStackSpeaker::set_mute_state(bool mute_state) {
  this->mute_state_ = mute_state;

#ifdef USE_AUDIO_DAC
  if (this->audio_dac_ != nullptr) {
    ESP_LOGD(TAG, "Speaker mute: %s -> audio_dac", mute_state ? "on" : "off");
    if (mute_state) {
      this->audio_dac_->set_mute_on();
    } else {
      this->audio_dac_->set_mute_off();
    }
  } else
#endif
  {
    ESP_LOGD(TAG, "Speaker mute: %s -> software_q31", mute_state ? "on" : "off");
    if (mute_state) {
      this->parent_->set_output_volume_q31(0);
    } else {
      this->parent_->set_output_volume(this->volume_);
    }
  }
}

void ESPAudioStackSpeaker::set_pause_state(bool pause_state) {
  this->pause_state_ = pause_state;
  this->parent_->set_speaker_paused(pause_state);
  ESP_LOGD(TAG, "Pause state: %s", pause_state ? "PAUSED" : "PLAYING");
}

void ESPAudioStackSpeaker::loop() {
  // Propagate I2S errors from parent audio task
  if (this->parent_->has_i2s_error()) {
    if (!this->i2s_error_latched_) {
      ESP_LOGE(TAG, "I2S error detected in audio task");
      this->status_set_error(LOG_STR("I2S write error in audio task"));
      this->i2s_error_latched_ = true;
    }
  } else if (this->i2s_error_latched_) {
    this->status_clear_error();
    this->i2s_error_latched_ = false;
    ESP_LOGI(TAG, "I2S audio path recovered");
  }

  const bool requested = this->listener_registered_.load(std::memory_order_acquire);

  if (requested && (this->state_ == speaker::STATE_STOPPED)) {
    this->state_ = speaker::STATE_STARTING;
  }

  if (!requested && (this->state_ == speaker::STATE_RUNNING)) {
    this->state_ = speaker::STATE_STOPPING;
  }

  switch (this->state_) {
    case speaker::STATE_STARTING:
      if (this->status_has_error() && !this->i2s_error_latched_) {
        break;
      }
      if (!this->listener_registered_.load(std::memory_order_acquire)) {
        if (this->parent_requested_) this->parent_->stop_speaker();
        this->parent_requested_ = false;
        this->state_ = speaker::STATE_STOPPED;
        break;
      }
      if (!this->parent_requested_) {
        this->parent_->start_speaker();
        this->parent_requested_ = true;
      }
      if (this->parent_->is_starting()) {
        this->disable_loop();
        break;
      }
      if (!this->parent_->is_running()) {
        ESP_LOGW(TAG, "Parent audio stack failed to start; aborting speaker start");
        this->parent_->stop_speaker();
        this->parent_requested_ = false;
        this->stop();
        this->state_ = speaker::STATE_STOPPED;
        break;
      }
      this->state_ = speaker::STATE_RUNNING;
      break;

    case speaker::STATE_RUNNING:
      // Non-blocking finish: drain buffer then stop
      if (this->finishing_ && !this->has_buffered_data()) {
        this->finishing_ = false;
        this->stop();
      } else if (!this->pause_state_ && this->timeout_.has_value() && !this->has_buffered_data() &&
                 (millis() - this->last_write_ms_) > this->timeout_.value()) {
        this->stop();
      }
      break;

    case speaker::STATE_STOPPING:
      this->parent_->stop_speaker();
      this->parent_requested_ = false;
      this->state_ = speaker::STATE_STOPPED;
      break;

    case speaker::STATE_STOPPED:
      this->disable_loop();
      break;
  }
}

}  // namespace esphome::esp_audio_stack

#endif  // USE_ESP32
