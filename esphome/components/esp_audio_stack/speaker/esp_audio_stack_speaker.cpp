#include "esp_audio_stack_speaker.h"

#ifdef USE_ESP32

#include <cinttypes>
#include <cmath>

#include "esphome/core/hal.h"
#include "esphome/core/log.h"
#include "esphome/core/version.h"

#if ESPHOME_VERSION_CODE >= VERSION_CODE(2026, 11, 0)
#define ESP_AUDIO_STACK_MULTI_DAC_API
#endif

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
  this->implicit_recovery_used_.store(false, std::memory_order_release);
  if (this->parent_->has_failed_session()) {
    this->parent_->start();
    this->enable_loop_soon_any_context();
  }

  // An explicit start may cancel a graceful drain. The loop commits readiness.
  auto draining = SpeakerAdmission::DRAINING;
  this->admission_.compare_exchange_strong(draining, SpeakerAdmission::STARTING, std::memory_order_acq_rel);
  if (this->pause_state_) {
    this->set_pause_state(false);
  }
  this->last_write_ms_ = millis();

  // Idempotent: register listener only once per stream session.
  bool expected = false;
  if (!this->listener_registered_.compare_exchange_strong(expected, true, std::memory_order_acq_rel,
                                                          std::memory_order_relaxed))
    return;

  this->admission_.store(SpeakerAdmission::STARTING, std::memory_order_release);
  this->enable_loop_soon_any_context();
}

void ESPAudioStackSpeaker::stop() {
  if (this->is_failed())
    return;
  this->implicit_recovery_used_.store(false, std::memory_order_release);

  if (this->pause_state_) {
    this->set_pause_state(false);
  }
  if (!this->listener_registered_.exchange(false, std::memory_order_acq_rel))
    return;
  this->admission_.store(SpeakerAdmission::STOPPING, std::memory_order_release);
  this->parent_->notify_speaker_writers_(true);
  this->enable_loop_soon_any_context();
}

void ESPAudioStackSpeaker::finish() {
  if (!this->listener_registered_.load(std::memory_order_acquire)) return;
  // Accepted PCM drains through the existing hardware completion path.
  if (this->pause_state_) {
    this->set_pause_state(false);
  }
  this->admission_.store(SpeakerAdmission::DRAINING, std::memory_order_release);
  this->parent_->notify_speaker_writers_(true);
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

  if (this->parent_->has_failed_session() && this->listener_registered_.load(std::memory_order_acquire)) {
    if (!this->implicit_recovery_used_.exchange(true, std::memory_order_acq_rel)) {
      this->parent_->start();
      this->enable_loop_soon_any_context();
    }
  } else if (!this->listener_registered_.load(std::memory_order_acquire) &&
             this->admission_.load(std::memory_order_acquire) == SpeakerAdmission::STOPPED) {
    this->start();
  }

  const size_t written = this->parent_->play_with_admission_(data, length, ticks_to_wait, &this->admission_);
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
  speaker::Speaker::set_volume(volume);

#ifdef USE_AUDIO_DAC
#ifdef ESP_AUDIO_STACK_MULTI_DAC_API
  if (this->has_audio_dac_()) {
#else
  if (this->audio_dac_ != nullptr) {
#endif
    ESP_LOGD(TAG, "Speaker set_volume: %.3f -> audio_dac", volume);
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
  speaker::Speaker::set_mute_state(mute_state);

#ifdef USE_AUDIO_DAC
#ifdef ESP_AUDIO_STACK_MULTI_DAC_API
  if (this->has_audio_dac_()) {
#else
  if (this->audio_dac_ != nullptr) {
#endif
    ESP_LOGD(TAG, "Speaker mute: %s -> audio_dac", mute_state ? "on" : "off");
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

  if (this->parent_->has_failed_session()) {
    this->parent_requested_ = false;
    this->admission_.store(SpeakerAdmission::STOPPED, std::memory_order_release);
    this->parent_->notify_speaker_writers_(true);
    this->state_ = speaker::STATE_STOPPED;
    this->disable_loop();
    return;
  }
  if (this->state_ == speaker::STATE_RUNNING && this->parent_->is_starting()) {
    this->parent_requested_ = false;
    this->admission_.store(SpeakerAdmission::STARTING, std::memory_order_release);
    this->state_ = speaker::STATE_STARTING;
  }

  const bool requested = this->listener_registered_.load(std::memory_order_acquire);

  if (requested && (this->state_ == speaker::STATE_STOPPED)) {
    auto stopped = SpeakerAdmission::STOPPED;
    this->admission_.compare_exchange_strong(stopped, SpeakerAdmission::STARTING, std::memory_order_acq_rel);
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
        this->admission_.store(SpeakerAdmission::STOPPED, std::memory_order_release);
        if (this->parent_requested_) this->parent_->stop_speaker();
        this->parent_requested_ = false;
        this->state_ = speaker::STATE_STOPPED;
        break;
      }
      if (!this->parent_requested_) {
#ifdef USE_ESP_AUDIO_STACK_AMP_SETTLE
        // Board power policy must be sampled before start_speaker's GPIO
        // callback cancels the idle-powerdown script. A warm restart must
        // still honor an unfinished cold-start interval from this facade.
        const uint32_t settle_ms = this->amplifier_settle_time_.value_or(0);
        if (settle_ms != 0 || !this->amplifier_settle_started_.load(std::memory_order_relaxed)) {
          this->amplifier_settle_started_.store(false, std::memory_order_release);
          this->amplifier_settle_ms_.store(settle_ms, std::memory_order_relaxed);
        }
#endif
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
#ifdef USE_ESP_AUDIO_STACK_AMP_SETTLE
      if (const uint32_t settle_ms = this->amplifier_settle_ms_.load(std::memory_order_relaxed); settle_ms != 0) {
        // An asynchronous parent opens I2S before invoking the GPIO callback.
        // Start the interval only after that completion, never at its request.
        if (!this->amplifier_settle_started_.load(std::memory_order_relaxed)) {
          this->amplifier_settle_started_ms_.store(millis(), std::memory_order_relaxed);
          this->amplifier_settle_started_.store(true, std::memory_order_release);
        }
        // Unsigned elapsed time handles millis wrap and long stopped periods.
        if (static_cast<uint32_t>(millis() - this->amplifier_settle_started_ms_.load(std::memory_order_relaxed)) < settle_ms)
          break;
        this->amplifier_settle_ms_.store(0, std::memory_order_relaxed);
        this->amplifier_settle_started_.store(false, std::memory_order_release);
      }
#endif
      // The idle budget starts when hardware is ready, not while opening it.
      this->last_write_ms_ = millis();
      this->state_ = speaker::STATE_RUNNING;
      if (auto starting = SpeakerAdmission::STARTING;
          this->admission_.compare_exchange_strong(starting, SpeakerAdmission::READY, std::memory_order_acq_rel))
        this->parent_->notify_speaker_writers_();
      break;

    case speaker::STATE_RUNNING:
      // Non-blocking finish: drain buffer then stop
      if (auto starting = SpeakerAdmission::STARTING;
          this->admission_.compare_exchange_strong(starting, SpeakerAdmission::READY, std::memory_order_acq_rel))
        this->parent_->notify_speaker_writers_();
      if (this->admission_.load(std::memory_order_acquire) == SpeakerAdmission::DRAINING && !this->has_buffered_data()) {
        this->stop();
      } else if (!this->pause_state_ && this->timeout_.has_value() && !this->has_buffered_data() &&
                 (millis() - this->last_write_ms_) > this->timeout_.value()) {
        this->stop();
      }
      break;

    case speaker::STATE_STOPPING:
      this->admission_.store(SpeakerAdmission::STOPPED, std::memory_order_release);
      this->parent_->stop_speaker();
      this->parent_requested_ = false;
      this->state_ = speaker::STATE_STOPPED;
      break;

    case speaker::STATE_STOPPED:
      if (auto stopping = SpeakerAdmission::STOPPING;
          this->admission_.compare_exchange_strong(stopping, SpeakerAdmission::STOPPED, std::memory_order_acq_rel))
        this->parent_->notify_speaker_writers_(true);
      this->disable_loop();
      break;
  }
}

}  // namespace esphome::esp_audio_stack

#endif  // USE_ESP32
