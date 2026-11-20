#include "esp_afe.h"
#include "../esp_audio_stack/audio_core_audio_utils.h"
#include "../esp_audio_stack/audio_core_scoped_lock.h"
#ifdef USE_ESP_AUDIO_STACK_AFE_OUTPUT_CAPTURE
#include "../esp_audio_stack/esp_audio_stack.h"
#endif

#ifdef USE_ESP32
#ifndef CLANG_TIDY

#include "esphome/core/helpers.h"
#include "esphome/core/log.h"

#include <esp_heap_caps.h>
#include <esp_err.h>
#include <esp_timer.h>
#include <freertos/idf_additions.h>

#include <algorithm>
#include <cmath>
#include <cstring>
#include <string>

#include "afe_manager_features.h"
#include "afe_diagnostic_helpers.h"

namespace esphome::esp_afe {

static const char *const TAG = "esp_afe";
static const TickType_t CONFIG_MUTEX_TIMEOUT = pdMS_TO_TICKS(250);
// Max wait for process() to finish its current frame when a config change
// needs to rebuild the AFE instance. ~1.5 frame periods at 16 kHz / 512
// samples.
static const TickType_t DRAIN_WAIT_TIMEOUT = pdMS_TO_TICKS(50);
static constexpr uint32_t REINIT_STAGE_WARN_US = 50000;



struct AfeModePreset {
  const char *name;
  int type;
  int mode;
};

static constexpr AfeModePreset AFE_MODE_PRESETS[] = {
    {"sr_low_cost", AFE_TYPE_SR, AFE_MODE_LOW_COST},         {"sr_high_perf", AFE_TYPE_SR, AFE_MODE_HIGH_PERF},
    {"voip_low_cost", AFE_TYPE_VC, AFE_MODE_LOW_COST},       {"voip_high_perf", AFE_TYPE_VC, AFE_MODE_HIGH_PERF},
    {"fd_low_cost", 3 /* AFE_TYPE_FD */, AFE_MODE_LOW_COST}, {"fd_high_perf", 3 /* AFE_TYPE_FD */, AFE_MODE_HIGH_PERF},
};

void EspAfe::set_input_format_override(const char *fmt) {
  if (fmt == nullptr || fmt[0] == '\0') {
    this->input_format_override_[0] = '\0';
    return;
  }
  std::strncpy(this->input_format_override_, fmt, sizeof(this->input_format_override_) - 1);
  this->input_format_override_[sizeof(this->input_format_override_) - 1] = '\0';
}


#ifdef USE_ESP_AFE_POST_AGC
bool EspAfe::prepare_post_afe_agc_() {
  this->release_post_afe_agc_();
  if (this->mic_num_ < 2 || !this->agc_enabled_.load(std::memory_order_relaxed))
    return true;
  this->post_afe_agc_ = esp_agc_open(AGC_MODE_2, 16000);
  if (this->post_afe_agc_ == nullptr) {
    ESP_LOGE(TAG, "Failed to create dual-mic post-AFE AGC");
    return false;
  }
  set_agc_config(this->post_afe_agc_, this->agc_compression_gain_, 1, this->agc_target_level_);
  this->reset_post_afe_agc_();
  return true;
}

void EspAfe::release_post_afe_agc_() {
  if (this->post_afe_agc_ != nullptr)
    esp_agc_close(this->post_afe_agc_);
  this->post_afe_agc_ = nullptr;
  this->reset_post_afe_agc_();
}

void EspAfe::reset_post_afe_agc_() {
  this->post_afe_agc_input_samples_ = 0;
  memset(this->post_afe_agc_output_, 0, sizeof(this->post_afe_agc_output_));
}

bool EspAfe::process_post_afe_agc_frame_(const int16_t *input, int16_t *output, size_t samples) {
  if (input == nullptr || output == nullptr)
    return false;
  if (this->post_afe_agc_ == nullptr) {
    if (input != output)
      memcpy(output, input, samples * sizeof(int16_t));
    return true;
  }
  bool success = true;
  for (size_t i = 0; i < samples; i++) {
    // Read before writing: process() deliberately uses the same frame buffer.
    const int16_t sample = input[i];
    const size_t slot = this->post_afe_agc_input_samples_;
    output[i] = this->post_afe_agc_output_[slot];
    this->post_afe_agc_input_[slot] = sample;
    if (++this->post_afe_agc_input_samples_ != kPostAfeAgcQuantumSamples)
      continue;
    const int ret = esp_agc_process(this->post_afe_agc_, this->post_afe_agc_input_,
                                    this->post_afe_agc_output_, kPostAfeAgcQuantumSamples, 16000);
    this->post_afe_agc_input_samples_ = 0;
    if (ret != ESP_AGC_SUCCESS) {
      // Preserve the same timeline on failure: bypass only this quantum,
      // without discarding samples or inserting another silence prebuffer.
      memcpy(this->post_afe_agc_output_, this->post_afe_agc_input_, sizeof(this->post_afe_agc_output_));
      success = false;
    }
  }
  return success;
}
#endif

void EspAfe::clear_process_busy_() {
  this->process_busy_.store(false, std::memory_order_seq_cst);
  TaskHandle_t waiter = this->process_drain_waiter_.load(std::memory_order_acquire);
  if (waiter != nullptr) {
    xTaskNotifyGive(waiter);
  }
}

bool EspAfe::quiesce_process_(TickType_t ticks_to_wait) {
  this->drain_request_.store(true, std::memory_order_seq_cst);
  bool drained = true;
  if (this->process_busy_.load(std::memory_order_seq_cst)) {
    TaskHandle_t waiter = xTaskGetCurrentTaskHandle();
    ulTaskNotifyTake(pdTRUE, 0);
    this->process_drain_waiter_.store(waiter, std::memory_order_release);
    do {
      if (this->process_busy_.load(std::memory_order_seq_cst)) ulTaskNotifyTake(pdTRUE, ticks_to_wait);
    } while (ticks_to_wait == portMAX_DELAY && this->process_busy_.load(std::memory_order_seq_cst));
    if (this->process_busy_.load(std::memory_order_seq_cst)) {
      ESP_LOGE(TAG, "Drain timeout waiting for process() to quiesce; keeping the live AFE instance");
      drained = false;
    }
    this->process_drain_waiter_.store(nullptr, std::memory_order_release);
  }
  if (!drained) this->drain_request_.store(false, std::memory_order_seq_cst);
  return drained;
}

bool EspAfe::recreate_instance_(bool require_same_frame_sizes) {
  const int64_t reinit_start_us = esp_timer_get_time();
  int64_t stage_start_us = reinit_start_us;
  auto begin_stage = [&](const char *stage) {
    (void) stage;
    stage_start_us = esp_timer_get_time();
  };
  auto log_stage = [&](const char *stage) {
    const int64_t now_us = esp_timer_get_time();
    const uint32_t stage_us = static_cast<uint32_t>(std::max<int64_t>(0, now_us - stage_start_us));
    if (stage_us >= REINIT_STAGE_WARN_US) {
      ESP_LOGW(TAG, "AFE reinit stage %s took %uus", stage, (unsigned) stage_us);
    }
    stage_start_us = now_us;
  };

  if (this->config_mutex_ == nullptr) {
    this->config_mutex_ = xSemaphoreCreateMutex();
    if (this->config_mutex_ == nullptr) {
      ESP_LOGE(TAG, "Failed to create config mutex");
      return false;
    }
  }

  esp_audio_stack::ScopedLock lock(this->config_mutex_, CONFIG_MUTEX_TIMEOUT);
  if (!lock) {
    ESP_LOGW(TAG, "Timed out waiting to rebuild AFE instance");
    return false;
  }

  // Drain protocol: seq_cst gives the request/busy atomics one total order.
  // That prevents the store-buffer false/false outcome that release/acquire
  // would still allow on different atomic objects.
  if (!this->quiesce_process_(DRAIN_WAIT_TIMEOUT)) return false;
  log_stage("drain");

  // From here on the ScopedLock auto-releases on every return path; only the
  // drain flag has to be reset before each return.
  auto release_drain = [this]() { this->drain_request_.store(false, std::memory_order_seq_cst); };

  // esp-sr FFT resources are global: only one AFE instance can exist.
  // Must destroy the previous instance before creating the next one.
  int old_process = this->process_chunksize_;
  int old_fetch = this->fetch_chunksize_;
  begin_stage("detach");
  AfeInstance old = this->detach_instance_();
  log_stage("detach");
  begin_stage("destroy");
  this->destroy_instance_(&old);
  log_stage("destroy");
  begin_stage("release_runtime");
  this->release_runtime_buffers_();
  log_stage("release_runtime");

  // If every user-facing feature is disabled there is nothing to build.
  // Stay in the torn-down state; process() will emit silence via the
  // afe_stopped_ fast path using the last-known frame spec.
  //
  // Guard: only take this shortcut once we have a cached spec to hand out
  // via frame_spec(). On the very first call (setup() before any successful
  // install) last_spec_* are zero; build the instance normally so downstream
  // learns the frame shape, then subsequent toggles can tear down cleanly.
  if (this->all_features_disabled_() && this->last_spec_process_size_.load(std::memory_order_acquire) > 0 &&
      this->last_spec_fetch_size_.load(std::memory_order_acquire) > 0) {
    bool was_running = !this->afe_stopped_.load(std::memory_order_acquire);
    this->afe_stopped_.store(true, std::memory_order_release);
    release_drain();
    if (was_running) {
      ESP_LOGI(TAG, "AFE stopped (all features disabled); output silenced");
    }
    return true;
  }

  AfeInstance next;
  begin_stage("build");
  if (!this->build_instance_(&next)) {
    log_stage("build_failed");
    ESP_LOGE(TAG, "Failed to build new AFE instance. AFE is DOWN until "
                  "successful rebuild.");
    release_drain();
    return false;
  }
  log_stage("build");

  if (require_same_frame_sizes && old_process > 0 && old_fetch > 0 &&
      (next.process_chunksize != old_process || next.fetch_chunksize != old_fetch)) {
    ESP_LOGW(TAG, "Reinit changed external frame sizes (%d/%d -> %d/%d), rejecting", old_process, old_fetch,
             next.process_chunksize, next.fetch_chunksize);
    this->destroy_instance_(&next);
    release_drain();
    return false;
  }

  // Compare against the last successfully-installed spec, not against the
  // (already-detached) `this->*_chunksize_` fields which are zero at this
  // point. Using the last_spec_* members means a rollback to the previous
  // config does not spuriously bump frame_spec_revision_, which would make
  // esp_audio_stack try to restart its audio task concurrently with our
  // fetch task recreation and race inside FreeRTOS.
  int new_mic_ch = this->afe_mic_channels_();
  const int old_mic_ch = this->last_spec_mic_ch_.load(std::memory_order_acquire);
  const int old_process_spec = this->last_spec_process_size_.load(std::memory_order_acquire);
  const int old_fetch_spec = this->last_spec_fetch_size_.load(std::memory_order_acquire);
  bool spec_changed = new_mic_ch != old_mic_ch || next.process_chunksize != old_process_spec ||
                      next.fetch_chunksize != old_fetch_spec;
  (void) old_process;
  (void) old_fetch;

  begin_stage("install");
  this->manager_epoch_.store(this->processing_request_.load(std::memory_order_acquire) >> 1,
                             std::memory_order_release);
  if (!this->install_instance_(&next)) {
    log_stage("install_failed");
    AfeInstance failed = this->detach_instance_();
    this->destroy_instance_(&failed);
    this->release_runtime_buffers_();
    release_drain();
    return false;
  }
  log_stage("install");

  this->last_spec_process_size_.store(this->process_chunksize_, std::memory_order_release);
  this->last_spec_fetch_size_.store(this->fetch_chunksize_, std::memory_order_release);
  this->last_spec_mic_ch_.store(new_mic_ch, std::memory_order_release);

  if (spec_changed) {
    // Release barrier ensures new frame_spec stores happen-before consumers
    // observe the bumped revision via acquire load.
    uint32_t new_rev = this->frame_spec_revision_.fetch_add(1, std::memory_order_release) + 1;
    ESP_LOGI(TAG,
             "Frame spec changed: mic_ch=%d->%d, process=%d, fetch=%d "
             "(revision %u, audio task will restart)",
             old_mic_ch, new_mic_ch, this->process_chunksize_, this->fetch_chunksize_, (unsigned) new_rev);
  }

  // First boot with every single-mic feature disabled: build once so
  // frame_spec() has a valid shape, then tear the manager back down. This
  // prevents an all-off AFE from acting like a raw passthrough.
  if (this->all_features_disabled_()) {
    AfeInstance idle = this->detach_instance_();
    this->destroy_instance_(&idle);
    this->release_runtime_buffers_();
    this->afe_stopped_.store(true, std::memory_order_release);
    release_drain();
    ESP_LOGI(TAG, "AFE stopped (all features disabled); output silenced");
    return true;
  }

  this->warmup_remaining_ = 3;
  this->input_ring_drop_.store(0, std::memory_order_relaxed);
  this->feed_ok_.store(0, std::memory_order_relaxed);
  this->feed_rejected_.store(0, std::memory_order_relaxed);
  this->fetch_ok_.store(0, std::memory_order_relaxed);
  this->fetch_timeout_.store(0, std::memory_order_relaxed);
  this->output_ring_drop_.store(0, std::memory_order_relaxed);
  this->feed_queue_frames_.store(0, std::memory_order_relaxed);
  this->feed_queue_peak_.store(0, std::memory_order_relaxed);
  this->fetch_queue_frames_.store(0, std::memory_order_relaxed);
  this->fetch_queue_peak_.store(0, std::memory_order_relaxed);
  this->process_us_last_.store(0, std::memory_order_relaxed);
  this->process_us_max_.store(0, std::memory_order_relaxed);
  this->feed_us_last_.store(0, std::memory_order_relaxed);
  this->feed_us_max_.store(0, std::memory_order_relaxed);
  this->fetch_us_last_.store(0, std::memory_order_relaxed);
  this->fetch_us_max_.store(0, std::memory_order_relaxed);
  this->ringbuf_free_pct_.store(1.0f, std::memory_order_relaxed);
  this->voice_present_.store(false, std::memory_order_relaxed);
  this->input_volume_dbfs_.store(-120.0f, std::memory_order_relaxed);
  this->output_rms_dbfs_.store(-120.0f, std::memory_order_relaxed);
  // Backend pointers, runtime rings and the published frame shape are now a
  // coherent instance. Readers may query readiness without touching pointers.
  this->instance_ready_.store(true, std::memory_order_release);
  // Clear the stopped flag: a live instance is running. Paired with the
  // acquire load in process() so the hot path sees the transition cleanly.
  bool was_stopped = this->afe_stopped_.exchange(false, std::memory_order_release);
  if (was_stopped) {
    ESP_LOGI(TAG, "AFE restarted (feature re-enabled)");
  }
  // Release drain: new instance is fully installed. The manager's tasks
  // already exist and are either started by install_instance_ or left suspended
  // until the next mic consumer attaches. process() can resume real work on
  // the next frame.
  release_drain();
  const uint32_t total_us = static_cast<uint32_t>(std::max<int64_t>(0, esp_timer_get_time() - reinit_start_us));
  if (total_us >= REINIT_STAGE_WARN_US) {
    ESP_LOGW(TAG, "AFE reinit total took %uus (type=%d mode=%d)", (unsigned) total_us, this->afe_type_,
             this->afe_mode_);
  }
  return true;
}

bool EspAfe::set_aec_enabled_runtime_(bool enabled) {
  const bool old_value = this->aec_enabled_.load(std::memory_order_relaxed);
  if (old_value == enabled) {
    if (!this->aec_enabled_.is_applied() && this->reconfigure_task_handle_ != nullptr &&
        (this->processing_request_.load(std::memory_order_acquire) & 1U) != 0) {
      this->activity_pending_.store(true, std::memory_order_release);
      xTaskNotifyGive(this->reconfigure_task_handle_);
    }
    return true;
  }
  if (this->config_mutex_ == nullptr) {
    ESP_LOGW(TAG, "AEC toggle requested before setup");
    return false;
  }

  if (this->reconfigure_task_handle_ != nullptr && !this->processing_owner_current_()) {
    // A retired/pending acquisition has no active AEC to control. The existing
    // owner applies this intent before reopening capture; AEC alone is not a
    // microphone consumer and must not rebuild an idle manager here.
    this->aec_enabled_.request(enabled);
    this->activity_pending_.store(true, std::memory_order_release);
    xTaskNotifyGive(this->reconfigure_task_handle_);
    return true;
  }

  // AFE currently torn down (all features were off). Flip the flag and let
  // recreate_instance_ rebuild (or stay stopped if still all-off).
  if (this->afe_stopped_.load(std::memory_order_acquire)) {
    const uint32_t request = this->aec_enabled_.request(enabled);
    if (!enabled) {
      return true;  // nothing to rebuild
    }
    if (this->recreate_instance_(false)) return true;
    this->aec_enabled_.restore_if_current(request, old_value);
    return false;
  }

  // Hold the config mutex only across the enable/disable call. The
  // potential teardown via recreate_instance_ takes the same mutex
  // itself; calling it inside the lock would recurse on a non-recursive
  // mutex and time out.
  bool needs_rebuild = false;
  {
    esp_audio_stack::ScopedLock lock(this->config_mutex_, CONFIG_MUTEX_TIMEOUT);
    if (!lock) {
      ESP_LOGW(TAG, "Timed out waiting to toggle AEC");
      return false;
    }
    if (!this->is_initialized()) {
      ESP_LOGW(TAG, "AEC toggle requested without a ready AFE instance");
      return false;
    }

    const uint32_t previous_request = this->aec_enabled_.snapshot();
    int ret = 0;
    const char *backend = "unknown";
#ifdef USE_ESP_AFE_GMF_PATH
    if (this->afe_manager_ != nullptr) {
      ret = static_cast<int>(esp_gmf_afe_manager_enable_features(this->afe_manager_, ESP_AFE_FEATURE_AEC, enabled));
#if defined(USE_ESP_AUDIO_STACK_TELEMETRY) || defined(USE_ESP_AUDIO_STACK_CAPTURE)
      this->diagnostic_aec_instance_id_ = this->diagnostic_instance_id_;
      this->diagnostic_aec_requested_ = enabled;
      this->diagnostic_aec_ret_ = ret;
#endif
      backend = "GMF AFE manager";
    } else
#endif
    {
      ESP_LOGW(TAG, "AFE %s AEC requested without a compiled backend", enabled ? "enable" : "disable");
      return false;
    }
    if (ret != static_cast<int>(enabled)) {
      ESP_LOGW(TAG, "AFE %s AEC not acknowledged (ret=%d)", enabled ? "enable" : "disable", ret);
      return false;
    }

    ESP_LOGI(TAG, "AEC %s via %s", enabled ? "enabled" : "disabled", backend);
    uint32_t applied_request;
    if (this->aec_enabled_.publish_if_current(previous_request, enabled, &applied_request)) {
      this->aec_enabled_.acknowledge(applied_request);
    } else {
      this->aec_enabled_.invalidate();
      this->activity_pending_.store(true, std::memory_order_release);
      if (this->reconfigure_task_handle_ != nullptr) xTaskNotifyGive(this->reconfigure_task_handle_);
    }

    // Live toggle left AFE running. If the user just turned AEC off and
    // every other feature was already off, we should stop the whole
    // pipeline. Compute the decision while holding the lock so the
    // feature flags don't tear under a concurrent toggle.
    needs_rebuild = (!enabled && this->all_features_disabled_());
  }

  if (needs_rebuild) {
    this->recreate_instance_(false);  // no-features path tears down inside
  }
  return true;
}

bool EspAfe::set_vad_enabled_runtime_(bool enabled) {
  const bool old_value = this->vad_enabled_.load(std::memory_order_relaxed);
  if (old_value == enabled) {
    return true;
  }
  if (this->config_mutex_ == nullptr) {
    ESP_LOGW(TAG, "VAD toggle requested before setup");
    return false;
  }

  if (this->reconfigure_task_handle_ != nullptr && !this->processing_owner_current_()) {
    // A retired or pending acquisition has no live VAD to toggle. Publish
    // intent without waiting for its owner: Audio Stack reconciles the
    // continuous-VAD consumer, and the same worker applies the new feature.
    this->vad_enabled_.store(enabled, std::memory_order_relaxed);
    this->voice_present_.store(false, std::memory_order_relaxed);
    this->activity_pending_.store(true, std::memory_order_release);
    xTaskNotifyGive(this->reconfigure_task_handle_);
    return true;
  }

  // AFE currently torn down (all features were off). Flip the flag and let
  // recreate_instance_ rebuild (or stay stopped if still all-off).
  if (this->afe_stopped_.load(std::memory_order_acquire)) {
    this->vad_enabled_.store(enabled, std::memory_order_relaxed);
    if (!enabled) {
      this->voice_present_.store(false, std::memory_order_relaxed);
      return true;
    }
    if (this->recreate_instance_(false)) return true;
    this->vad_enabled_.store(old_value, std::memory_order_relaxed);
    return false;
  }

  bool needs_rebuild = false;
  {
    esp_audio_stack::ScopedLock lock(this->config_mutex_, CONFIG_MUTEX_TIMEOUT);
    if (!lock) {
      ESP_LOGW(TAG, "Timed out waiting to toggle VAD");
      return false;
    }
    if (!this->is_initialized()) {
      ESP_LOGW(TAG, "VAD toggle requested without a ready AFE instance");
      return false;
    }

    int ret = 0;
    const char *backend = "unknown";
#ifdef USE_ESP_AFE_GMF_PATH
    if (this->afe_manager_ != nullptr) {
      ret = static_cast<int>(apply_vad_intent(this->afe_manager_, enabled));
      backend = "GMF AFE manager";
    } else
#endif
    {
      ESP_LOGW(TAG, "AFE %s VAD requested without a compiled backend", enabled ? "enable" : "disable");
      return false;
    }
    if (ret < 0) {
      ESP_LOGW(TAG, "AFE %s VAD failed (ret=%d)", enabled ? "enable" : "disable", ret);
      return false;
    }

    ESP_LOGI(TAG, "VAD %s via %s", enabled ? "enabled" : "disabled", backend);
    this->vad_enabled_.store(enabled, std::memory_order_relaxed);
    if (!enabled) {
      this->voice_present_.store(false, std::memory_order_relaxed);
    }
    needs_rebuild = (!enabled && this->all_features_disabled_());
  }

  if (needs_rebuild) {
    this->recreate_instance_(false);  // no-features path tears down inside
  }
  return true;
}

bool EspAfe::set_reinit_flag_(std::atomic<bool> &flag, bool enabled, const char *name) {
  const bool old_value = flag.load(std::memory_order_relaxed);
  if (old_value == enabled) {
    return true;
  }
  // ESP-SR can legitimately change the feed/fetch quantum when NS/AGC are
  // rebuilt. Accept that public contract and let frame_spec_revision_ make the
  // duplex task restart with the new shape.
  constexpr bool allow_frame_change = true;
  // AFE torn down (all-off) and a feature is coming back: commit the flag and
  // rebuild via recreate_instance_.
  if (this->afe_stopped_.load(std::memory_order_acquire)) {
    flag.store(enabled, std::memory_order_relaxed);
    if (!enabled) {
      return true;  // stays torn down
    }
    if (this->recreate_instance_(false)) return true;
    flag.store(old_value, std::memory_order_relaxed);
    return false;
  }
  if (this->config_mutex_ == nullptr) {
    // Before setup: commit immediately; the first build will use the value.
    flag.store(enabled, std::memory_order_relaxed);
    ESP_LOGD(TAG, "Deferring %s=%s until AFE is initialized", name, enabled ? "true" : "false");
    return true;
  }
  // Staged config: set flag, rebuild, rollback on failure.
  // Flag must be set before rebuild because build_instance_ reads it.
  // The mutex in recreate_instance_ ensures process() either sees the old
  // instance or the new one, never a mix.
  //
  flag.store(enabled, std::memory_order_relaxed);
  ESP_LOGI(TAG, "Applying %s=%s (rebuild, frame_size_change=%s)", name, enabled ? "true" : "false",
           allow_frame_change ? "allowed" : "locked");
  if (this->recreate_instance_(!allow_frame_change)) {
    return true;
  }
  // Rebuild failed: restore flag and try to rebuild with the previous config.
  ESP_LOGW(TAG, "Failed to apply %s=%s, rolling back", name, enabled ? "true" : "false");
  flag.store(old_value, std::memory_order_relaxed);
  if (!this->recreate_instance_(!allow_frame_change)) {
    ESP_LOGE(TAG, "Rollback also failed for %s, AFE is down", name);
  }
  return false;
}

void EspAfe::setup() {
  if (!this->recreate_instance_(false)) {
    this->mark_failed();
    return;
  }
  if (!this->start_reconfigure_task_()) {
    AfeInstance failed = this->detach_instance_();
    this->destroy_instance_(&failed);
    this->release_runtime_buffers_();
    this->mark_failed();
    return;
  }
  if (this->activity_pending_.load(std::memory_order_acquire)) xTaskNotifyGive(this->reconfigure_task_handle_);
  ESP_LOGI(TAG, "AFE setup complete, runtime prepared and idle (waiting for "
                "mic consumer)");
}

FrameSpec EspAfe::frame_spec() const {
  FrameSpec spec;
  spec.sample_rate = 16000;
  spec.mic_channels = this->last_spec_mic_ch_.load(std::memory_order_acquire);
  spec.ref_channels = 1;
  spec.supports_planar_input = true;
  // The live instance fields are detached on the reconfigure task. Publish a
  // stable snapshot instead so frame_spec() remains race-free during rebuilds.
  spec.input_samples = this->last_spec_process_size_.load(std::memory_order_acquire);
  spec.output_samples = this->last_spec_fetch_size_.load(std::memory_order_acquire);
  return spec;
}

FeatureControl EspAfe::feature_control(AudioFeature feature) const {
  switch (feature) {
    case AudioFeature::AEC:
      return FeatureControl::LIVE_TOGGLE;
    case AudioFeature::VAD:
      return FeatureControl::RESTART_REQUIRED;
    case AudioFeature::NS:
    case AudioFeature::AGC:
      return FeatureControl::RESTART_REQUIRED;
    case AudioFeature::SE:
      return this->mic_num_ >= 2 ? FeatureControl::BOOT_ONLY : FeatureControl::NOT_SUPPORTED;
    default:
      return FeatureControl::NOT_SUPPORTED;
  }
}

bool EspAfe::set_feature(AudioFeature feature, bool enabled) {
  switch (feature) {
    case AudioFeature::AEC:
      return enabled ? this->enable_aec() : this->disable_aec();
    case AudioFeature::SE:
      return false;
    case AudioFeature::NS:
      return enabled ? this->enable_ns() : this->disable_ns();
    case AudioFeature::VAD:
      return enabled ? this->enable_vad() : this->disable_vad();
    case AudioFeature::AGC:
      return enabled ? this->enable_agc() : this->disable_agc();
    default:
      return false;
  }
}

bool EspAfe::reconfigure(int type, int mode) {
  int old_type = this->afe_type_;
  int old_mode = this->afe_mode_;
  this->afe_type_ = type;
  this->afe_mode_ = mode;
  if (this->recreate_instance_(false)) {
    const char *type_name = (this->afe_type_ == AFE_TYPE_SR) ? "SR" : (this->afe_type_ == 3) ? "FD" : "VC";
    ESP_LOGI(TAG, "AFE reconfigured: type=%s, mode=%s", type_name,
             this->afe_mode_ == AFE_MODE_LOW_COST ? "LOW_COST" : "HIGH_PERF");
    return true;
  }
  // Rollback on failure: restore the previous config and rebuild to avoid
  // leaving the DSP permanently non-functional.
  ESP_LOGW(TAG,
           "reconfigure: new type=%d mode=%d build failed, rolling back to "
           "type=%d mode=%d",
           type, mode, old_type, old_mode);
  this->afe_type_ = old_type;
  this->afe_mode_ = old_mode;
  if (!this->recreate_instance_(false)) {
    ESP_LOGE(TAG, "reconfigure: rollback rebuild ALSO failed - AFE is DOWN");
  }
  return false;
}

bool EspAfe::reinit_by_name(const std::string &name) { return this->reinit_by_name(name.c_str()); }

bool EspAfe::reinit_by_name(const char *name) {
  if (name == nullptr) {
    ESP_LOGW(TAG, "Unknown AFE mode: (null)");
    return false;
  }
  for (const auto &preset : AFE_MODE_PRESETS) {
    if (std::strcmp(name, preset.name) == 0) {
      return this->reconfigure(preset.type, preset.mode);
    }
  }

  ESP_LOGW(TAG, "Unknown AFE mode: %s", name);
  return false;
}

bool EspAfe::start_reconfigure_task_() {
  if (this->reconfigure_done_ == nullptr) {
    this->reconfigure_done_ = xSemaphoreCreateBinaryStatic(&this->reconfigure_done_storage_);
    if (this->reconfigure_done_ == nullptr) return false;
  }
  if (this->reconfigure_queue_ == nullptr) {
    this->reconfigure_queue_ = xQueueCreateStatic(1, sizeof(ReconfigureRequest), this->reconfigure_queue_storage_,
                                                  &this->reconfigure_queue_struct_);
    if (this->reconfigure_queue_ == nullptr) {
      ESP_LOGE(TAG, "Failed to create AFE reconfigure queue");
      return false;
    }
  }
  if (this->reconfigure_task_handle_ != nullptr) {
    return true;
  }

  this->reconfigure_task_running_.store(true, std::memory_order_release);
  xSemaphoreTake(this->reconfigure_done_, 0);
  auto start_owner = [this]([[maybe_unused]] UBaseType_t caps) {
    const BaseType_t core = this->reconfigure_task_core_ < 0 ? tskNO_AFFINITY : this->reconfigure_task_core_;
#if configSUPPORT_STATIC_ALLOCATION
    return xTaskCreatePinnedToCoreWithCaps(&EspAfe::reconfigure_task_trampoline, "afe_reinit",
        this->reconfigure_task_stack_size_, this, this->reconfigure_task_priority_,
        &this->reconfigure_task_handle_, core, caps) == pdPASS;
#else
    return xTaskCreatePinnedToCore(&EspAfe::reconfigure_task_trampoline, "afe_reinit",
        this->reconfigure_task_stack_size_, this, this->reconfigure_task_priority_,
        &this->reconfigure_task_handle_, core) == pdPASS;
#endif
  };
  // This configuration owner parks between requests and never performs flash
  // operations. Keep its large stack out of internal RAM when IDF permits it;
  // IDF releases a failed attempt before the single internal fallback.
  bool started = false;
#if configSUPPORT_STATIC_ALLOCATION && CONFIG_FREERTOS_TASK_CREATE_ALLOW_EXT_MEM
  if (this->reconfigure_task_stack_memory_ != 1 && heap_caps_get_total_size(MALLOC_CAP_SPIRAM) != 0)
    started = start_owner(MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
#endif
  this->reconfigure_task_stack_external_ = started;
  if (!started && (this->reconfigure_task_stack_memory_ == 2 ||
                   !start_owner(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT))) {
    ESP_LOGE(TAG, "Failed to create AFE reconfigure task");
    this->reconfigure_task_running_.store(false, std::memory_order_release);
    return false;
  }
  return true;
}

bool EspAfe::request_reinit_by_name(const std::string &name) { return this->request_reinit_by_name(name.c_str()); }

bool EspAfe::request_reinit_by_name(const char *name) {
  if (name == nullptr || name[0] == '\0') {
    ESP_LOGW(TAG, "Cannot queue empty AFE mode");
    return false;
  }
  if (std::strcmp(name, this->get_mode_name().c_str()) == 0 &&
      !this->reconfigure_busy_.load(std::memory_order_acquire)) {
    this->last_reconfigure_ok_.store(true, std::memory_order_release);
    ESP_LOGI(TAG, "AFE already in mode %s", name);
    return true;
  }
  if (!this->start_reconfigure_task_()) {
    return false;
  }
  ReconfigureRequest req{};
  std::strncpy(req.mode, name, sizeof(req.mode) - 1);
  const bool already_pending =
      this->reconfigure_busy_.load(std::memory_order_acquire) || uxQueueMessagesWaiting(this->reconfigure_queue_) > 0;
  this->reconfigure_busy_.store(true, std::memory_order_release);
  this->last_reconfigure_ok_.store(false, std::memory_order_release);
  if (xQueueOverwrite(this->reconfigure_queue_, &req) != pdTRUE) {
    ESP_LOGW(TAG, "AFE reconfigure queue write failed; rejecting %s", name);
    this->reconfigure_busy_.store(false, std::memory_order_release);
    return false;
  }
  xTaskNotifyGive(this->reconfigure_task_handle_);
  ESP_LOGI(TAG, "%s AFE reconfigure to %s", already_pending ? "Updated pending" : "Queued", req.mode);
  return true;
}

bool EspAfe::is_reconfigure_idle() const {
  if (this->activity_pending_.load(std::memory_order_acquire)) return false;
  if (this->reconfigure_queue_ == nullptr) {
    return !this->reconfigure_busy_.load(std::memory_order_acquire);
  }
  return !this->reconfigure_busy_.load(std::memory_order_acquire) &&
         uxQueueMessagesWaiting(this->reconfigure_queue_) == 0;
}

void EspAfe::reconfigure_task_trampoline(void *arg) {
  auto *self = static_cast<EspAfe *>(arg);
  const auto done = self->reconfigure_done_;
  self->reconfigure_task_loop_();
  // No owner access after completion. The destructor deletes the parked task
  // before releasing the component, its mutex or the GMF instance.
  xSemaphoreGive(done);
  vTaskSuspend(nullptr);
}

void EspAfe::reconfigure_task_loop_() {
  ReconfigureRequest req{};
  while (this->reconfigure_task_running_.load(std::memory_order_acquire)) {
    // A process-drain notification shares this task's notification slot.
    // Pending intent/queue entries remain authoritative if a drain consumed
    // that wake; only park when there is no work left to reconcile.
    if (!this->activity_pending_.load(std::memory_order_acquire) &&
        uxQueueMessagesWaiting(this->reconfigure_queue_) == 0) {
      ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
    }
    if (!this->reconfigure_task_running_.load(std::memory_order_acquire)) break;
    this->reconfigure_busy_.store(true, std::memory_order_release);
    bool ok = true;
    if (xQueueReceive(this->reconfigure_queue_, &req, 0) == pdTRUE) {
      ESP_LOGI(TAG, "AFE async reconfigure begin: %s", req.mode);
      ok = this->reinit_by_name(req.mode);
      ESP_LOGI(TAG, "AFE async reconfigure %s: %s", ok ? "done" : "failed", req.mode);
    }
    if (this->activity_pending_.exchange(false, std::memory_order_acq_rel)) {
      ok = this->service_processing_request_() && ok;
    }
    this->last_reconfigure_ok_.store(ok, std::memory_order_release);
    this->reconfigure_busy_.store(false, std::memory_order_release);
  }
}

bool EspAfe::enable_aec() { return this->set_aec_enabled_runtime_(true); }
bool EspAfe::disable_aec() { return this->set_aec_enabled_runtime_(false); }
bool EspAfe::enable_ns() { return this->set_reinit_flag_(this->ns_enabled_, true, "ns_enabled"); }
bool EspAfe::disable_ns() { return this->set_reinit_flag_(this->ns_enabled_, false, "ns_enabled"); }
bool EspAfe::enable_vad() { return this->set_vad_enabled_runtime_(true); }
bool EspAfe::disable_vad() { return this->set_vad_enabled_runtime_(false); }
bool EspAfe::enable_agc() {
#ifndef USE_ESP_AFE_POST_AGC
  if (this->mic_num_ >= 2) {
    ESP_LOGW(TAG, "Dual-mic AGC not compiled; enable post_afe_agc_support in YAML");
    return false;
  }
#endif
  return this->set_reinit_flag_(this->agc_enabled_, true, "agc_enabled");
}
bool EspAfe::disable_agc() { return this->set_reinit_flag_(this->agc_enabled_, false, "agc_enabled"); }

bool EspAfe::start_pipeline_() {
#ifdef USE_ESP_AFE_GMF_PATH
  if (this->afe_manager_ == nullptr) {
    return false;
  }
  if (this->afe_pipeline_running_) {
    return true;
  }
  this->staged_input_samples_ = 0;
  this->drain_feed_input_ring_();
  if (this->fetch_output_ring_) {
    this->fetch_output_ring_->reset();
  }
  this->reset_output_prebuffer_();
  this->feed_queue_frames_.store(0, std::memory_order_relaxed);
  this->fetch_queue_frames_.store(0, std::memory_order_relaxed);
#ifdef USE_ESP_AFE_POST_AGC
  this->reset_post_afe_agc_();
#endif
  // Installing the read callback resumes the manager, so install output first.
  if (esp_gmf_afe_manager_set_result_cb(this->afe_manager_, &EspAfe::manager_result_cb_, this) != ESP_GMF_ERR_OK) {
    return false;
  }
  if (esp_gmf_afe_manager_set_read_cb(this->afe_manager_, &EspAfe::manager_read_cb_, this) != ESP_GMF_ERR_OK) {
    esp_gmf_afe_manager_set_result_cb(this->afe_manager_, nullptr, nullptr);
    return false;
  }
  this->afe_pipeline_running_ = true;
  this->afe_pipeline_paused_ = false;
  return true;
#else
  return false;
#endif
}

bool EspAfe::pause_pipeline_() {
#ifdef USE_ESP_AFE_GMF_PATH
  if (this->afe_manager_ != nullptr) {
    if (esp_gmf_afe_manager_suspend(this->afe_manager_, true) != ESP_GMF_ERR_OK) {
      return false;
    }
    this->flush_pipeline_before_stop_();
    // These setters wait for any in-flight callback before clearing its context.
    const esp_gmf_err_t read_ret = esp_gmf_afe_manager_set_read_cb(this->afe_manager_, nullptr, nullptr);
    const esp_gmf_err_t result_ret = esp_gmf_afe_manager_set_result_cb(this->afe_manager_, nullptr, nullptr);
    if (read_ret != ESP_GMF_ERR_OK || result_ret != ESP_GMF_ERR_OK) {
      return false;
    }
  }
#endif
  this->afe_pipeline_running_ = false;
  this->afe_pipeline_paused_ = true;
  return true;
}

void EspAfe::stop_pipeline_() {
  if (!this->pause_pipeline_()) {
    ESP_LOGW(TAG, "AFE manager pause failed during stop");
  }
  this->afe_pipeline_running_ = false;
  this->afe_pipeline_paused_ = false;
#ifdef USE_ESP_AFE_GMF_PATH
  this->drain_feed_input_ring_();
#endif
  if (this->fetch_output_ring_) {
    this->fetch_output_ring_->reset();
  }
  this->reset_output_prebuffer_();
  this->feed_queue_frames_.store(0, std::memory_order_relaxed);
  this->fetch_queue_frames_.store(0, std::memory_order_relaxed);
}


void EspAfe::set_processing_active(bool active) {
  uint32_t previous = this->processing_request_.load(std::memory_order_acquire);
  uint32_t next;
  do {
    if (((previous & 1U) != 0) == active) {
      // A failed explicit acquisition may retry; unchanged live consumers do
      // not rebuild or wake the owner every frame.
      if (!active || this->processing_active_.load(std::memory_order_acquire) ||
          this->activity_pending_.load(std::memory_order_acquire) ||
          this->reconfigure_busy_.load(std::memory_order_acquire)) return;
      break;
    }
    next = active ? previous | 1U : (previous + 2U) & ~1U;
  } while (!this->processing_request_.compare_exchange_weak(previous, next, std::memory_order_acq_rel));
  if (!active) {
    this->processing_active_.store(false, std::memory_order_release);
    this->voice_present_.store(false, std::memory_order_relaxed);
  }
  this->activity_pending_.store(true, std::memory_order_release);
  if (this->reconfigure_task_handle_ != nullptr) xTaskNotifyGive(this->reconfigure_task_handle_);
}

bool EspAfe::retire_processing_session_() {
  esp_audio_stack::ScopedLock lock(this->config_mutex_, CONFIG_MUTEX_TIMEOUT);
  if (!lock || !this->quiesce_process_(DRAIN_WAIT_TIMEOUT)) return false;
  this->processing_active_.store(false, std::memory_order_release);
  AfeInstance old = this->detach_instance_();
  // Official destroy joins feed/fetch; callback detachment alone cannot join
  // fetch() because the manager looks up its callback after fetch returns.
  this->destroy_instance_(&old);
  this->release_runtime_buffers_();
  this->voice_present_.store(false, std::memory_order_relaxed);
  this->manager_epoch_.store(this->processing_request_.load(std::memory_order_acquire) >> 1,
                             std::memory_order_release);
  this->drain_request_.store(false, std::memory_order_seq_cst);
  return true;
}

bool EspAfe::service_processing_request_() {
  const uint32_t request = this->processing_request_.load(std::memory_order_acquire);
  if ((request & 1U) == 0 || this->manager_epoch_.load(std::memory_order_acquire) != (request >> 1)) {
    if (!this->retire_processing_session_()) return false;
  }
  if ((this->processing_request_.load(std::memory_order_acquire) & 1U) == 0 ||
      (this->afe_stopped_.load(std::memory_order_acquire) && this->all_features_disabled_())) return true;
  if (!this->is_initialized() && !this->recreate_instance_(false)) return false;

  esp_audio_stack::ScopedLock lock(this->config_mutex_, CONFIG_MUTEX_TIMEOUT);
  if (!lock) return false;
  const uint32_t latest = this->processing_request_.load(std::memory_order_acquire);
  if ((latest & 1U) == 0 || this->manager_epoch_.load(std::memory_order_acquire) != (latest >> 1)) return true;
#ifdef USE_ESP_AFE_GMF_PATH
  const uint32_t aec_request = this->aec_enabled_.snapshot();
  const bool aec_requested = AecIntent::enabled(aec_request);
  const int aec_ret = apply_aec_intent(this->afe_manager_, aec_requested);
  if (aec_ret != static_cast<int>(aec_requested)) {
    ESP_LOGW(TAG, "AFE acquisition could not apply AEC intent (ret=%d)", aec_ret);
    return false;
  }
  this->aec_enabled_.acknowledge(aec_request);
  // A VAD intent may arrive while this acquisition is being installed. Its
  // pending notification guarantees another pass if it changes after this
  // load; only the existing config owner touches the ready manager here.
  if (apply_vad_intent(this->afe_manager_, this->vad_enabled_.load(std::memory_order_relaxed)) < 0) {
    ESP_LOGW(TAG, "AFE acquisition could not apply VAD intent");
    return false;
  }
#endif
  if (!this->start_pipeline_()) {
    this->processing_active_.store(false, std::memory_order_release);
    return false;
  }
  this->processing_active_.store(true, std::memory_order_release);
  ESP_LOGI(TAG, "AFE acquisition ready (epoch=%u)", (unsigned) (latest >> 1));
  return true;
}

bool EspAfe::prepare_runtime_() {
  // Resources follow the newly installed manager. afe_stopped_ still describes
  // the previous published instance until the complete rebuild commits.
  this->log_memory_snapshot_("before_afe_prepare_runtime");
#ifdef USE_ESP_AFE_GMF_PATH
  if (!this->prepare_feed_input_ring_()) {
    return false;
  }
#endif
  if (!this->prepare_fetch_output_ring_()) {
    return false;
  }
  this->log_memory_snapshot_("after_afe_prepare_runtime");
  ESP_LOGI(TAG, "AFE runtime prepared (GMF feed/fetch rings)");
  return true;
}

void EspAfe::release_runtime_buffers_() {
#ifdef USE_ESP_AFE_POST_AGC
  this->release_post_afe_agc_();
#endif
  this->fetch_output_ring_.reset();
  this->reset_output_prebuffer_();
#ifdef USE_ESP_AFE_GMF_PATH
  this->staged_feed_slot_ = nullptr;
  this->staged_input_samples_ = 0;
  this->feed_input_ring_ = nullptr;
  if (this->feed_input_ring_storage_ != nullptr) {
    heap_caps_free(this->feed_input_ring_storage_);
    this->feed_input_ring_storage_ = nullptr;
  }
  if (this->feed_input_ring_struct_ != nullptr) {
    heap_caps_free(this->feed_input_ring_struct_);
    this->feed_input_ring_struct_ = nullptr;
  }
#endif
}

EspAfe::~EspAfe() {
  this->processing_active_.store(false, std::memory_order_release);
  if (this->reconfigure_task_handle_ != nullptr) {
    this->reconfigure_task_running_.store(false, std::memory_order_release);
    xTaskNotifyGive(this->reconfigure_task_handle_);
    xSemaphoreTake(this->reconfigure_done_, portMAX_DELAY);
#if configSUPPORT_STATIC_ALLOCATION
    // IDF suspends and joins a remote-core task before freeing its stack/TCB.
    vTaskDeleteWithCaps(this->reconfigure_task_handle_);
#else
    vTaskDelete(this->reconfigure_task_handle_);
#endif
    this->reconfigure_task_handle_ = nullptr;
  }
  // process() does not take config_mutex_. Join its admitted frame before
  // freeing anything, as well as the reconfigure owner joined above.
  if (this->config_mutex_ != nullptr) {
    {
      esp_audio_stack::ScopedLock lock(this->config_mutex_, portMAX_DELAY);
      this->quiesce_process_(portMAX_DELAY);
      AfeInstance instance = this->detach_instance_();
      this->destroy_instance_(&instance);
      this->release_runtime_buffers_();
    }
    vSemaphoreDelete(this->config_mutex_);
    this->config_mutex_ = nullptr;
  } else {
    AfeInstance instance = this->detach_instance_();
    this->destroy_instance_(&instance);
    this->release_runtime_buffers_();
  }
}

}  // namespace esphome::esp_afe

#endif  // !CLANG_TIDY
#endif  // USE_ESP32
