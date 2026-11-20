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

namespace esphome::esp_afe {

static const char *const TAG = "esp_afe";

// GMF 1.1 exposes this placement explicitly. The published compatibility fork
// predating that field already uses the same fixed P4/internal, S3/PSRAM policy.
// Keep source compatibility until coordinated source pins can be published.
template<typename Setting>
static auto configure_gmf_stack_memory(Setting &setting, bool internal, int)
    -> decltype(setting.stack_in_internal = internal, void()) {
  setting.stack_in_internal = internal;
}
template<typename Setting> static void configure_gmf_stack_memory(Setting &, bool, long) {}

// ESP-SR's VOIP echo suppressor exceeds 4 KiB on the feed task. GMF's
// generic 3 KiB default is suitable for other graphs, not this AEC path.
// Keep the larger allocation local to the algorithm that requires it and
// preserve explicitly configured larger stacks.
static int effective_feed_task_stack_size(int requested, const afe_config_t *config) {
  if (config != nullptr && config->aec_init &&
      (config->aec_mode == AEC_MODE_VOIP_LOW_COST || config->aec_mode == AEC_MODE_VOIP_HIGH_PERF)) {
    return std::max(requested, 8192);
  }
  return requested;
}

aec_mode_t EspAfe::derive_aec_mode_() const {
  const bool high = (this->afe_mode_ == AFE_MODE_HIGH_PERF);
  switch (this->afe_type_) {
    case AFE_TYPE_SR:
      return high ? AEC_MODE_SR_HIGH_PERF : AEC_MODE_SR_LOW_COST;
    case 3:  // AFE_TYPE_FD (esp-sr 2.4+, value matches the upstream enum)
      // AEC_MODE_FD_HIGH_PERF = 6, AEC_MODE_FD_LOW_COST = 5.
      return static_cast<aec_mode_t>(high ? 6 : 5);
    default:  // AFE_TYPE_VC
      return high ? AEC_MODE_VOIP_HIGH_PERF : AEC_MODE_VOIP_LOW_COST;
  }
}

int EspAfe::afe_mic_channels_() const {
  if (this->mic_num_ < 2) {
    return 1;
  }
  return 2;
}

void EspAfe::configure_afe_features_(afe_config_t *cfg, int afe_mic_channels) {
  // Keep AEC allocated so the GMF feature control can toggle it without
  // changing frame sizes or replacing the active audio pipeline.
  cfg->aec_init = true;
  cfg->aec_filter_length = this->aec_filter_length_;
  cfg->aec_mode = this->derive_aec_mode_();
  cfg->aec_nlp_level = static_cast<aec_nlp_level_t>(this->aec_nlp_level_);

  // On dual-mic AFE builds SE/BSS is structural. Espressif's AFE contract
  // downgrades to first-mic-only when SE is disabled, so dual-mic devices keep
  // it initialized and enabled by design.
  if (afe_mic_channels >= 2) {
    this->se_enabled_.store(true, std::memory_order_relaxed);
  }
  cfg->se_init = afe_mic_channels >= 2;

  cfg->ns_init = this->ns_enabled_.load(std::memory_order_relaxed);
  cfg->ns_model_name = nullptr;
  cfg->afe_ns_mode = AFE_NS_MODE_WEBRTC;

  // ESP-SR/GMF can toggle VAD at runtime only if the VAD block exists in the
  // AFE instance. Keep it structurally initialized and gate the live state via
  // esp_gmf_afe_manager_enable_features() before the pipeline starts.
  cfg->vad_init = true;
  cfg->vad_mode = static_cast<vad_mode_t>(this->vad_mode_);
  cfg->vad_model_name = nullptr;
  cfg->vad_min_speech_ms = this->vad_min_speech_ms_;
  cfg->vad_min_noise_ms = this->vad_min_noise_ms_;
  cfg->vad_delay_ms = this->vad_delay_ms_;
  cfg->vad_mute_playback = this->vad_mute_playback_;
  cfg->vad_enable_channel_trigger = this->vad_enable_channel_trigger_;

  cfg->wakenet_init = false;
  cfg->wakenet_model_name = nullptr;
  cfg->wakenet_model_name_2 = nullptr;

  const bool use_post_afe_agc = afe_mic_channels >= 2 && this->agc_enabled_.load(std::memory_order_relaxed);
  // ESP-SR 2.5.3 still omits AGC from its effective 2MIC graph. Do not claim
  // an internal stage exists; dual-mic GMF output is processed explicitly.
  cfg->agc_init = this->agc_enabled_.load(std::memory_order_relaxed) && !use_post_afe_agc;
  cfg->agc_mode = AFE_AGC_MODE_WEBRTC;
  cfg->agc_compression_gain_db = this->agc_compression_gain_;
  cfg->agc_target_level_dbfs = this->agc_target_level_;

  // esp-sr spawns its internal worker using these fields. Single-mic MR
  // runs WebRTC NS/AGC in the feed path and needs extra internal queue
  // headroom; this matches the previously validated ESP-SR direct topology.
  cfg->afe_perferred_core = this->task_core_;
  cfg->afe_perferred_priority = this->task_priority_;
  cfg->afe_ringbuf_size = (afe_mic_channels <= 1 && this->ringbuf_size_ < 16) ? 16 : this->ringbuf_size_;
  cfg->memory_alloc_mode = static_cast<afe_memory_alloc_mode_t>(this->memory_alloc_mode_);
  cfg->afe_linear_gain = this->afe_linear_gain_;
  cfg->debug_init = false;
  // Single-mic MR was validated with fixed_first_channel=true. Keep dual-mic
  // on the current processed-output behavior so BSS/raw-output selection is
  // unchanged for WS3/P4.
  cfg->fixed_first_channel = afe_mic_channels <= 1;

  afe_config_check(cfg);
}

bool EspAfe::build_instance_(AfeInstance *instance) {
  if (instance == nullptr) {
    return false;
  }

  const int afe_mic_channels = this->afe_mic_channels_();
  if (afe_mic_channels >= 2) {
    this->se_enabled_.store(true, std::memory_order_relaxed);
  }
  // Stack-allocated input format string. Default preserves the established
  // "MR" / "MMR" shape; an optional override selects Espressif's documented
  // "MMNR" dual-mic layout without changing the transport-facing mic channel
  // count.
  char fmt[5];
  if (afe_mic_channels >= 2 && this->input_format_override_[0] != '\0') {
    std::strncpy(fmt, this->input_format_override_, sizeof(fmt) - 1);
    fmt[sizeof(fmt) - 1] = '\0';
  } else {
    for (int i = 0; i < afe_mic_channels && i < 2; i++)
      fmt[i] = 'M';
    fmt[afe_mic_channels] = 'R';
    fmt[afe_mic_channels + 1] = '\0';
  }

  afe_config_t *cfg =
      afe_config_init(fmt, nullptr, static_cast<afe_type_t>(this->afe_type_), static_cast<afe_mode_t>(this->afe_mode_));

  if (cfg == nullptr) {
    ESP_LOGE(TAG, "afe_config_init failed for input_format=%s type=%d mode=%d", fmt, this->afe_type_, this->afe_mode_);
    return false;
  }

  this->configure_afe_features_(cfg, afe_mic_channels);

  const esp_afe_sr_iface_t *handle = esp_afe_handle_from_config(cfg);
  if (handle == nullptr) {
    ESP_LOGE(TAG, "esp_afe_handle_from_config returned NULL");
    afe_config_free(cfg);
    return false;
  }

#ifdef USE_ESP_AFE_GMF_PATH
  esp_gmf_afe_manager_cfg_t manager_cfg{};
  manager_cfg.afe_cfg = cfg;
  manager_cfg.feed_task_setting.prio = this->feed_task_priority_;
  manager_cfg.feed_task_setting.core = this->feed_task_core_;
  manager_cfg.feed_task_setting.stack_size = effective_feed_task_stack_size(this->feed_task_stack_size_, cfg);
  if (manager_cfg.feed_task_setting.stack_size != this->feed_task_stack_size_) {
    ESP_LOGI(TAG, "VOIP AEC feed stack: %d requested, %u bytes required", this->feed_task_stack_size_,
             (unsigned) manager_cfg.feed_task_setting.stack_size);
  }
  manager_cfg.fetch_task_setting.prio = this->fetch_task_priority_;
  manager_cfg.fetch_task_setting.core = this->fetch_task_core_;
  manager_cfg.fetch_task_setting.stack_size = this->fetch_task_stack_size_;
#ifdef CONFIG_IDF_TARGET_ESP32P4
  constexpr bool afe_stacks_internal = true;
#else
  constexpr bool afe_stacks_internal = false;
#endif
  configure_gmf_stack_memory(manager_cfg.feed_task_setting, afe_stacks_internal, 0);
  configure_gmf_stack_memory(manager_cfg.fetch_task_setting, afe_stacks_internal, 0);
  manager_cfg.read_cb = nullptr;
  manager_cfg.read_ctx = nullptr;
  manager_cfg.result_cb = nullptr;
  manager_cfg.result_ctx = nullptr;

  esp_gmf_afe_manager_handle_t manager = nullptr;
  esp_gmf_err_t manager_ret = esp_gmf_afe_manager_create(&manager_cfg, &manager);
  if (manager_ret != ESP_GMF_ERR_OK || manager == nullptr) {
    ESP_LOGE(TAG, "esp_gmf_afe_manager_create failed (ret=%d)", static_cast<int>(manager_ret));
    afe_config_free(cfg);
    return false;
  }
  size_t feed_chunk_size = 0;
  uint8_t total_channels_u8 = 0;
  if (esp_gmf_afe_manager_get_chunk_size(manager, &feed_chunk_size) != ESP_GMF_ERR_OK ||
      esp_gmf_afe_manager_get_input_ch_num(manager, &total_channels_u8) != ESP_GMF_ERR_OK || feed_chunk_size == 0 ||
      total_channels_u8 == 0 || total_channels_u8 != cfg->pcm_config.total_ch_num ||
      cfg->pcm_config.mic_num != afe_mic_channels || cfg->pcm_config.ref_num != 1 ||
      cfg->pcm_config.mic_ids == nullptr || cfg->pcm_config.ref_ids == nullptr) {
    ESP_LOGE(TAG, "Failed to query GMF AFE manager frame shape");
    esp_gmf_afe_manager_destroy(manager);
    afe_config_free(cfg);
    return false;
  }

  int feed_chunksize = static_cast<int>(feed_chunk_size);
  // DSP feed granularity must not make the full-duplex I/O task wait for a
  // whole algorithm frame before servicing playback. Reuse the transport
  // quantum and existing staging/byte stream for larger native AFE blocks.
  // Keep this public quantum stable when features are reconfigured later.
  const int published_quantum = this->last_spec_process_size_.load(std::memory_order_acquire);
  int process_chunksize = published_quantum > 0
                             ? published_quantum
                             : std::min(feed_chunksize, static_cast<int>(esp_audio_stack::DEFAULT_AUDIO_FRAME_SAMPLES));
  int fetch_chunksize = process_chunksize;
  // Use official API for feed channel count instead of config struct (more
  // robust if esp-sr changes internal channel mapping in future versions).
  int total_channels = static_cast<int>(total_channels_u8);
  // GMF owns the staging reservation even when transport and DSP sizes differ.
  // The fallback buffer is retained only for a non-GMF backend.
#ifdef USE_ESP_AFE_GMF_PATH
  constexpr bool needs_feed_staging = false;
#else
  const bool needs_feed_staging = process_chunksize != feed_chunksize;
#endif
  int16_t *feed_buf = nullptr;
  if (needs_feed_staging) {
    const size_t feed_bytes = static_cast<size_t>(feed_chunksize) * total_channels * sizeof(int16_t);
    const uint32_t feed_buf_caps =
        this->feed_buf_in_psram_ ? (MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT) : (MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    feed_buf = static_cast<int16_t *>(heap_caps_aligned_alloc(16, feed_bytes, feed_buf_caps));
    if (feed_buf == nullptr && this->feed_buf_in_psram_) {
      ESP_LOGW(TAG,
               "staged feed_buf (%u bytes) fell back to internal RAM (PSRAM "
               "full/unavailable)",
               static_cast<unsigned>(feed_bytes));
      feed_buf = static_cast<int16_t *>(heap_caps_aligned_alloc(16, feed_bytes, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    }
    if (feed_buf == nullptr) {
      ESP_LOGE(TAG, "Failed to allocate staged feed buffer (%u bytes)", static_cast<unsigned>(feed_bytes));
      esp_gmf_afe_manager_destroy(manager);
      afe_config_free(cfg);
      return false;
    }
  }

  instance->manager = manager;
  instance->config = cfg;
  instance->feed_buf = feed_buf;
  instance->feed_chunksize = feed_chunksize;
  instance->fetch_chunksize = fetch_chunksize;
  instance->process_chunksize = process_chunksize;
  instance->total_channels = total_channels;

  return true;
#else
  ESP_LOGE(TAG, "Dual-mic GMF AFE path was not compiled in");
  afe_config_free(cfg);
  return false;
#endif
}

void EspAfe::destroy_instance_(AfeInstance *instance) {
  if (instance == nullptr) {
    return;
  }
#ifdef USE_ESP_AFE_GMF_PATH
  if (instance->manager != nullptr) {
    esp_gmf_afe_manager_destroy(instance->manager);
    instance->manager = nullptr;
  }
#endif
  if (instance->config != nullptr) {
    afe_config_free(instance->config);
    instance->config = nullptr;
  }
  if (instance->feed_buf != nullptr) {
    heap_caps_free(instance->feed_buf);
    instance->feed_buf = nullptr;
  }
  instance->feed_chunksize = 0;
  instance->fetch_chunksize = 0;
  instance->process_chunksize = 0;
  instance->total_channels = 0;
}

bool EspAfe::install_instance_(AfeInstance *instance) {
#if defined(USE_ESP_AUDIO_STACK_TELEMETRY) || defined(USE_ESP_AUDIO_STACK_CAPTURE)
  ++this->diagnostic_instance_id_;
#endif
#ifdef USE_ESP_AUDIO_STACK_CAPTURE
  this->last_fetch_trigger_channel_id_.store(-1, std::memory_order_relaxed);
  this->last_fetch_raw_data_channels_.store(-1, std::memory_order_relaxed);
  this->last_fetch_data_size_.store(-1, std::memory_order_relaxed);
#endif
#ifdef USE_ESP_AFE_GMF_PATH
  this->afe_manager_ = instance->manager;
#endif
  this->afe_config_ = instance->config;
  this->feed_buf_ = instance->feed_buf;
  this->feed_chunksize_ = instance->feed_chunksize;
  this->fetch_chunksize_ = instance->fetch_chunksize;
  this->process_chunksize_ = instance->process_chunksize;
  this->total_channels_ = instance->total_channels;
  this->staged_input_samples_ = 0;
  this->reset_output_prebuffer_();

#ifdef USE_ESP_AFE_GMF_PATH
  instance->manager = nullptr;
#endif
  instance->config = nullptr;
  instance->feed_buf = nullptr;
  instance->feed_chunksize = 0;
  instance->fetch_chunksize = 0;
  instance->process_chunksize = 0;
  instance->total_channels = 0;

  auto cleanup_failed_install = [this]() -> bool {
    AfeInstance failed;
#ifdef USE_ESP_AFE_GMF_PATH
    failed.manager = this->afe_manager_;
#endif
    failed.config = this->afe_config_;
    failed.feed_buf = this->feed_buf_;
    failed.feed_chunksize = this->feed_chunksize_;
    failed.fetch_chunksize = this->fetch_chunksize_;
    failed.process_chunksize = this->process_chunksize_;
    failed.total_channels = this->total_channels_;
#ifdef USE_ESP_AFE_GMF_PATH
    this->afe_manager_ = nullptr;
#endif
    this->afe_config_ = nullptr;
    this->afe_pipeline_running_ = false;
    this->afe_pipeline_paused_ = false;
    this->feed_buf_ = nullptr;
    this->feed_chunksize_ = 0;
    this->fetch_chunksize_ = 0;
    this->process_chunksize_ = 0;
    this->total_channels_ = 0;
    this->destroy_instance_(&failed);
    return false;
  };

  if (!this->prepare_runtime_()) {
    ESP_LOGE(TAG, "Failed to prepare AFE runtime buffers");
    return cleanup_failed_install();
  }
#ifdef USE_ESP_AFE_POST_AGC
  if (!this->prepare_post_afe_agc_()) {
    return cleanup_failed_install();
  }
#endif

#ifdef USE_ESP_AFE_GMF_PATH
  const uint32_t aec_request = this->aec_enabled_.snapshot();
  const bool aec_requested = AecIntent::enabled(aec_request);
  const int aec_ret = esp_gmf_afe_manager_enable_features(this->afe_manager_, ESP_AFE_FEATURE_AEC, aec_requested);
#if defined(USE_ESP_AUDIO_STACK_TELEMETRY) || defined(USE_ESP_AUDIO_STACK_CAPTURE)
  this->diagnostic_aec_instance_id_ = this->diagnostic_instance_id_;
  this->diagnostic_aec_requested_ = aec_requested;
  this->diagnostic_aec_ret_ = aec_ret;
#endif
  // ESP-SR returns the applied state: 0 is OFF and 1 is ON, not ESP_OK.
  if (aec_ret != static_cast<int>(aec_requested)) {
    ESP_LOGE(TAG, "AFE AEC state not acknowledged: requested=%d ret=%d", static_cast<int>(aec_requested), aec_ret);
    return cleanup_failed_install();
  }
  this->aec_enabled_.acknowledge(aec_request);
  if (apply_vad_intent(this->afe_manager_, this->vad_enabled_.load(std::memory_order_relaxed)) < 0) {
    ESP_LOGE(TAG, "Failed to apply AFE feature state");
    return cleanup_failed_install();
  }

  // Runtime buffers are already prepared above. Apply the configured feature
  // state before resuming GMF tasks so an active reconfigure never runs even
  // one frame with stale AEC/VAD state.
  const uint32_t request = this->processing_request_.load(std::memory_order_acquire);
  const bool requested = (request & 1U) != 0 &&
                         this->manager_epoch_.load(std::memory_order_acquire) == (request >> 1);
  this->processing_active_.store(requested, std::memory_order_release);
  if (requested) {
    if (!this->start_pipeline_()) {
      this->processing_active_.store(false, std::memory_order_release);
      ESP_LOGE(TAG, "Reconfigure: AFE manager failed to start");
      return cleanup_failed_install();
    }
  }

  return true;
#else
  ESP_LOGE(TAG, "No usable AFE backend was compiled for this instance");
  return cleanup_failed_install();
#endif
}

EspAfe::AfeInstance EspAfe::detach_instance_() {
  // The drain handshake has quiesced process(). Detach manager callbacks before
  // handing ownership to destroy_instance_ or reusing the output buffers.
  this->processing_active_.store(false, std::memory_order_release);
  this->aec_enabled_.invalidate();
  this->stop_pipeline_();
  this->instance_ready_.store(false, std::memory_order_release);

  AfeInstance instance;
#ifdef USE_ESP_AFE_GMF_PATH
  instance.manager = this->afe_manager_;
#endif
  instance.config = this->afe_config_;
  instance.feed_buf = this->feed_buf_;
  instance.feed_chunksize = this->feed_chunksize_;
  instance.fetch_chunksize = this->fetch_chunksize_;
  instance.process_chunksize = this->process_chunksize_;
  instance.total_channels = this->total_channels_;

#ifdef USE_ESP_AFE_GMF_PATH
  this->afe_manager_ = nullptr;
#endif
  this->afe_config_ = nullptr;
  this->afe_pipeline_running_ = false;
  this->afe_pipeline_paused_ = false;
  this->feed_buf_ = nullptr;
  this->feed_chunksize_ = 0;
  this->fetch_chunksize_ = 0;
  this->process_chunksize_ = 0;
  this->total_channels_ = 0;
  this->staged_input_samples_ = 0;
  this->reset_output_prebuffer_();

  return instance;
}

}  // namespace esphome::esp_afe

#endif  // !CLANG_TIDY
#endif  // USE_ESP32
