#pragma once

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>

#include "audio_core_audio_utils.h"

namespace esphome::esp_audio_stack {

// Owned by the audio task. Sample one microphone frame per half second of
// time without playback. The median ignores brief disturbances like a door slam.
class AdaptivePlaybackVolume {
 public:
  void configure(float quiet_dbfs, float loud_dbfs, float max_reduction_db) {
    quiet_dbfs_ = quiet_dbfs;
    loud_dbfs_ = loud_dbfs;
    max_reduction_db_ = max_reduction_db;
  }

  void sync_switch(uint32_t epoch) {
    if (epoch_ == epoch)
      return;
    epoch_ = epoch;
    reset_estimate_();
  }

  void observe(uint32_t now_ms, const int16_t *mic, size_t samples, bool speaker_active) {
    if (!(epoch_ & 1U))
      return;
    if (speaker_active) {
      // A parked audio owner may resume directly into playback, with no idle
      // capture in between. A stale room estimate must not lower that stream.
      // Continuous playback keeps its measured gain until it finishes.
      if (count_ != 0 && now_ms - last_sample_ms_ > 1500 &&
          (!recent_playback_ || now_ms - last_playback_ms_ > 1500))
        reset_estimate_();
      last_playback_ms_ = now_ms;
      recent_playback_ = true;
      return;
    }
    if (recent_playback_ && now_ms - last_playback_ms_ < 500)
      return;
    recent_playback_ = false;
    // After a long pause, rebuild the room estimate using current observations.
    // Missing capture is unknown, not evidence of a quiet room. Use the user's
    // level until a complete new window exists, even if no consumer is running.
    if (count_ != 0 && now_ms - last_sample_ms_ > 1500)
      reset_estimate_();
    if (mic == nullptr || samples == 0 || (count_ != 0 && now_ms - last_sample_ms_ < 500))
      return;
    last_sample_ms_ = now_ms;
    levels_[next_] = compute_rms_dbfs_i16(mic, samples);
    next_ = (next_ + 1) % levels_.size();
    if (count_ < levels_.size())
      ++count_;
    if (count_ < levels_.size())
      return;

    auto sorted = levels_;
    std::sort(sorted.begin(), sorted.end());
    const float room = 0.5f * (sorted[7] + sorted[8]);
    const float fraction = std::clamp((loud_dbfs_ - room) / (loud_dbfs_ - quiet_dbfs_), 0.0f, 1.0f);
    // Apply at most 2 dB per reading. Playback holds the last measured level.
    const float step = std::clamp(max_reduction_db_ * fraction - reduction_db_, -2.0f, 2.0f);
    if (std::abs(step) >= 0.25f || (step != 0.0f && (fraction == 0.0f || fraction == 1.0f)))
      set_reduction_(reduction_db_ + step);
  }

  int32_t apply_to(int32_t gain) const {
    if (factor_q31_ == INT32_MAX)
      return gain;
    return static_cast<int32_t>((static_cast<int64_t>(gain) * factor_q31_ + (1LL << 30)) >> 31);
  }

 private:
  void reset_estimate_() {
    count_ = 0;
    next_ = 0;
    set_reduction_(0.0f);
  }

  void set_reduction_(float db) {
    reduction_db_ = db;
    factor_q31_ = db <= 0.0f ? INT32_MAX : static_cast<int32_t>(std::lround(std::pow(10.0, -db / 20.0) * INT32_MAX));
  }

  std::array<float, 16> levels_{};
  uint32_t last_sample_ms_{0};
  uint32_t last_playback_ms_{0};
  bool recent_playback_{false};
  uint8_t next_{0};
  uint8_t count_{0};
  uint32_t epoch_{0};
  float quiet_dbfs_{-50.0f};
  float loud_dbfs_{-30.0f};
  float max_reduction_db_{18.0f};
  float reduction_db_{0.0f};
  int32_t factor_q31_{INT32_MAX};
};

}  // namespace esphome::esp_audio_stack
