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

#include "afe_diagnostic_helpers.h"

namespace esphome::esp_afe {

static const char *const TAG = "esp_afe";

[[maybe_unused]] static const char *aec_nlp_level_name(int level) {
  switch (level) {
    case 0:
      return "NORMAL";
    case 1:
      return "AGGRESSIVE";
    case 2:
      return "VERY_AGGRESSIVE";
    default:
      return "UNKNOWN";
  }
}

void EspAfe::log_memory_snapshot_(const char *label) const {
  ESP_LOGI(TAG,
           "Memory[%s]: internal_free=%u largest_internal=%u dma_free=%u "
           "largest_dma=%u psram_free=%u",
           label, (unsigned) heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
           (unsigned) heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL),
           (unsigned) heap_caps_get_free_size(MALLOC_CAP_DMA),
           (unsigned) heap_caps_get_largest_free_block(MALLOC_CAP_DMA),
           (unsigned) heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
}

const char *EspAfe::memory_alloc_mode_to_str_() const {
  switch (this->memory_alloc_mode_) {
    case AFE_MEMORY_ALLOC_MORE_INTERNAL:
      return "MORE_INTERNAL";
    case AFE_MEMORY_ALLOC_INTERNAL_PSRAM_BALANCE:
      return "INTERNAL_PSRAM_BALANCE";
    case AFE_MEMORY_ALLOC_MORE_PSRAM:
      return "MORE_PSRAM";
    default:
      return "UNKNOWN";
  }
}

void EspAfe::dump_config() {
  ESP_LOGCONFIG(TAG, "ESP AFE (Audio Front End):");
  const char *type_name = (this->afe_type_ == AFE_TYPE_SR) ? "SR" : (this->afe_type_ == 3) ? "FD" : "VC";
  ESP_LOGCONFIG(TAG, "  Type: %s", type_name);
  ESP_LOGCONFIG(TAG, "  Mode: %s", this->afe_mode_ == AFE_MODE_LOW_COST ? "LOW_COST" : "HIGH_PERF");
  ESP_LOGCONFIG(TAG, "  Microphones: transport=%d, afe=%d", this->mic_num_, this->afe_mic_channels_());
  ESP_LOGCONFIG(TAG, "  AEC: %s (filter_length=%d, nlp=%s)",
                this->aec_enabled_.load(std::memory_order_relaxed) ? "ON" : "OFF", this->aec_filter_length_,
                aec_nlp_level_name(this->aec_nlp_level_));
  ESP_LOGCONFIG(TAG, "  AEC scratch: ~12 KB internal (always allocated, "
                     "live-toggle via esp-sr vtable; off-at-boot still pays the cost)");
  ESP_LOGCONFIG(TAG, "  NS: %s (WebRTC)", this->ns_enabled_.load(std::memory_order_relaxed) ? "ON" : "OFF");
  ESP_LOGCONFIG(TAG, "  VAD: %s (mode=%d, speech=%dms, noise=%dms, delay=%dms)",
                this->vad_enabled_.load(std::memory_order_relaxed) ? "ON" : "OFF", this->vad_mode_,
                this->vad_min_speech_ms_, this->vad_min_noise_ms_, this->vad_delay_ms_);
  ESP_LOGCONFIG(TAG, "  Continuous VAD Background Input: %s", this->continuous_vad_ ? "ON" : "OFF");
  const bool agc_enabled = this->agc_enabled_.load(std::memory_order_relaxed);
  ESP_LOGCONFIG(TAG, "  AGC: %s%s (gain=%ddB, target=-%ddBFS)", agc_enabled ? "ON" : "OFF",
                agc_enabled && this->mic_num_ >= 2 ? " (post-AFE WebRTC)" : "", this->agc_compression_gain_,
                this->agc_target_level_);
  if (this->mic_num_ >= 2) {
    ESP_LOGCONFIG(TAG, "  Speech Enhancement: %s", this->is_se_enabled() ? "ON (structural dual-mic)" : "OFF");
  } else {
    ESP_LOGCONFIG(TAG, "  Speech Enhancement: unavailable (mic_num < 2)");
  }
  ESP_LOGCONFIG(TAG, "  Input format override: %s",
                this->input_format_override_[0] ? this->input_format_override_ : "auto");
  if (this->mic_num_ >= 2) {
    ESP_LOGCONFIG(TAG, "  AEC-off dual-mic fallback: unavailable on official "
                       "GMF manager callback path");
  }
  ESP_LOGCONFIG(TAG, "  Alloc: %s, linear_gain=%.2f", this->memory_alloc_mode_to_str_(), this->afe_linear_gain_);
  ESP_LOGCONFIG(TAG, "  SE Task: core=%d, priority=%d, ringbuf=%d", this->task_core_, this->task_priority_,
                this->ringbuf_size_);
  ESP_LOGCONFIG(TAG,
                "  GMF Manager: feed core=%d prio=%d stack=%d, fetch core=%d "
                "prio=%d stack=%d",
                this->feed_task_core_, this->feed_task_priority_,
                effective_feed_task_stack_size(this->feed_task_stack_size_, this->afe_config_), this->fetch_task_core_,
                this->fetch_task_priority_, this->fetch_task_stack_size_);
  ESP_LOGCONFIG(TAG,
                "  Process: %d samples, Feed: %d samples, Fetch: %d samples, "
                "Channels: %d",
                this->process_chunksize_, this->feed_chunksize_, this->fetch_chunksize_, this->total_channels_);
  ESP_LOGCONFIG(TAG, "  Processed output prebuffer: %u frame(s)",
                static_cast<unsigned>(this->output_prebuffer_frames_));
  ESP_LOGCONFIG(TAG, "  Initialized: %s", this->is_initialized() ? "YES" : "NO");
  ESP_LOGCONFIG(TAG, "  Reconfigure task: core=%d priority=%d stack=%u memory=%s minimum_free=%u",
                this->reconfigure_task_core_, this->reconfigure_task_priority_,
                (unsigned) this->reconfigure_task_stack_size_,
                this->reconfigure_task_handle_ == nullptr ? "not started" :
                  this->reconfigure_task_stack_external_ ? "PSRAM" : "internal",
                this->reconfigure_task_handle_ == nullptr ? 0U :
                  (unsigned) uxTaskGetStackHighWaterMark(this->reconfigure_task_handle_));
}

ProcessorTelemetry EspAfe::telemetry() const {
  ProcessorTelemetry t;
  t.frame_count = this->process_frame_count_.load(std::memory_order_relaxed);
  t.glitch_count = this->process_output_miss_.load(std::memory_order_relaxed);
  t.voice_present = this->is_voice_present();
  t.input_volume_dbfs = this->input_volume_dbfs_.load(std::memory_order_relaxed);
  t.output_rms_dbfs = this->output_rms_dbfs_.load(std::memory_order_relaxed);
  t.ringbuf_free_pct = this->ringbuf_free_pct_.load(std::memory_order_relaxed);
  t.input_ring_drop = this->input_ring_drop_.load(std::memory_order_relaxed);
  t.feed_ok = this->feed_ok_.load(std::memory_order_relaxed);
  t.feed_rejected = this->feed_rejected_.load(std::memory_order_relaxed);
  t.fetch_ok = this->fetch_ok_.load(std::memory_order_relaxed);
  t.fetch_timeout = this->fetch_timeout_.load(std::memory_order_relaxed);
  t.output_ring_drop = this->output_ring_drop_.load(std::memory_order_relaxed);
  t.feed_queue_frames = this->feed_queue_frames_.load(std::memory_order_relaxed);
  t.feed_queue_peak = this->feed_queue_peak_.load(std::memory_order_relaxed);
  t.fetch_queue_frames = this->fetch_queue_frames_.load(std::memory_order_relaxed);
  t.fetch_queue_peak = this->fetch_queue_peak_.load(std::memory_order_relaxed);
  t.process_us_last = this->process_us_last_.load(std::memory_order_relaxed);
  t.process_us_max = this->process_us_max_.load(std::memory_order_relaxed);
  t.feed_us_last = this->feed_us_last_.load(std::memory_order_relaxed);
  t.feed_us_max = this->feed_us_max_.load(std::memory_order_relaxed);
  t.fetch_us_last = this->fetch_us_last_.load(std::memory_order_relaxed);
  t.fetch_us_max = this->fetch_us_max_.load(std::memory_order_relaxed);
  return t;
}

void EspAfe::dump_diagnostics() {
#if defined(USE_ESP_AUDIO_STACK_TELEMETRY) || defined(USE_ESP_AUDIO_STACK_CAPTURE)
  // Copy under the configuration owner's mutex, never block the main loop on
  // a rebuild, and never retain a DSP/config pointer for later dereference.
  struct Snapshot {
    const void *manager{nullptr}, *config{nullptr};
    uint32_t instance{0}, control_instance{0}, request{0}, epoch{0};
    int control_ret{0}, features_ret{-1};
    bool requested{false}, control_requested{false}, gmf_aec{false};
    bool ready{false}, active{false}, drain{false}, stopped{false}, busy{false}, reconfigure_ok{false};
    bool cfg_valid{false}, aec_init{false}, se{false}, ns{false}, vad{false}, agc{false}, wakenet{false};
    int type{-1}, mode{-1}, aec_mode{-1}, filter{0}, nlp{-1};
    int channels{0}, microphones{0}, references{0}, mic0{-1}, mic1{-1}, ref{-1};
    int feed{0}, process{0}, fetch{0};
#ifdef USE_ESP_AUDIO_STACK_CAPTURE
    int fetch_trigger_channel{-1}, fetch_raw_channels{-1}, fetch_data_size{-1};
#endif
    float linear_gain{0.0f};
    ProcessorTelemetry counters{};
  } s;
  static_assert(sizeof(Snapshot) <= 256, "Keep requested AFE diagnostics bounded");
  if (this->config_mutex_ == nullptr) {
    ESP_LOGI(TAG, "AFE diagnostic: unavailable=not_setup");
    return;
  }
  {
    esp_audio_stack::ScopedLock lock(this->config_mutex_, 0);
    if (!lock) {
      ESP_LOGI(TAG, "AFE diagnostic: unavailable=owner_busy");
      return;
    }
    s.instance = this->diagnostic_instance_id_;
    s.control_instance = this->diagnostic_aec_instance_id_;
    s.control_requested = this->diagnostic_aec_requested_;
    s.control_ret = this->diagnostic_aec_ret_;
    s.config = this->afe_config_;
#ifdef USE_ESP_AFE_GMF_PATH
    s.manager = this->afe_manager_;
    if (this->afe_manager_ != nullptr) {
      esp_gmf_afe_manager_features_t features{};
      s.features_ret = esp_gmf_afe_manager_get_features(this->afe_manager_, &features);
      s.gmf_aec = features.aec;
    }
#endif
    s.requested = this->aec_enabled_.load(std::memory_order_relaxed);
    s.request = this->processing_request_.load(std::memory_order_acquire);
    s.epoch = this->manager_epoch_.load(std::memory_order_acquire);
    s.ready = this->is_initialized();
    s.active = this->processing_active_.load(std::memory_order_acquire);
    s.drain = this->drain_request_.load(std::memory_order_acquire);
    s.stopped = this->afe_stopped_.load(std::memory_order_acquire);
    s.busy = this->reconfigure_busy_.load(std::memory_order_acquire);
    s.reconfigure_ok = this->last_reconfigure_ok_.load(std::memory_order_acquire);
    s.feed = this->feed_chunksize_;
    s.process = this->process_chunksize_;
    s.fetch = this->fetch_chunksize_;
#ifdef USE_ESP_AUDIO_STACK_CAPTURE
    s.fetch_trigger_channel = this->last_fetch_trigger_channel_id_.load(std::memory_order_relaxed);
    s.fetch_raw_channels = this->last_fetch_raw_data_channels_.load(std::memory_order_relaxed);
    s.fetch_data_size = this->last_fetch_data_size_.load(std::memory_order_relaxed);
#endif
    if (this->afe_config_ != nullptr) {
      const auto &cfg = *this->afe_config_;
      s.cfg_valid = true;
      s.type = cfg.afe_type;
      s.mode = cfg.afe_mode;
      s.aec_init = cfg.aec_init;
      s.aec_mode = cfg.aec_mode;
      s.filter = cfg.aec_filter_length;
      s.nlp = cfg.aec_nlp_level;
      s.se = cfg.se_init;
      s.ns = cfg.ns_init;
      s.vad = cfg.vad_init;
      s.agc = cfg.agc_init;
      s.wakenet = cfg.wakenet_init;
      s.linear_gain = cfg.afe_linear_gain;
      const auto &pcm = cfg.pcm_config;
      s.channels = pcm.total_ch_num;
      s.microphones = pcm.mic_num;
      s.references = pcm.ref_num;
      if (pcm.mic_ids != nullptr && pcm.mic_num > 0) s.mic0 = pcm.mic_ids[0];
      if (pcm.mic_ids != nullptr && pcm.mic_num > 1) s.mic1 = pcm.mic_ids[1];
      if (pcm.ref_ids != nullptr && pcm.ref_num > 0) s.ref = pcm.ref_ids[0];
    }
    s.counters = this->telemetry();
  }
  // GMF get_features is its cached control acknowledgement, not an independent
  // getter into the opaque algorithm. Keep that distinction in the log label.
  ESP_LOGI(TAG, "AFE diagnostic: instance=%u manager=%p config=%p ready=%s active=%s drain=%s stopped=%s request=%u epoch=%u reconfigure_busy=%s reconfigure_ok=%s",
           (unsigned) s.instance, s.manager, s.config, YESNO(s.ready), YESNO(s.active), YESNO(s.drain), YESNO(s.stopped),
           (unsigned) s.request, (unsigned) s.epoch, YESNO(s.busy), YESNO(s.reconfigure_ok));
  ESP_LOGI(TAG, "AEC control: requested=%s last_instance=%u last_requested=%s last_ret=%d control_known=%s same_instance=%s gmf_query_ret=%d gmf_cached_aec=%s",
           YESNO(s.requested), (unsigned) s.control_instance, YESNO(s.control_requested), s.control_ret,
           YESNO(s.control_instance != 0),
           YESNO(s.control_instance != 0 && s.manager != nullptr && s.control_instance == s.instance),
           s.features_ret, YESNO(s.gmf_aec));
  ESP_LOGI(TAG, "AFE checked_config: available=%s type=%d mode=%d aec_init=%s aec_mode=%d filter=%d nlp=%d se=%s ns=%s vad=%s agc=%s wakenet=%s linear_gain=%.3f",
           YESNO(s.cfg_valid), s.type, s.mode, YESNO(s.aec_init), s.aec_mode, s.filter, s.nlp,
           YESNO(s.se), YESNO(s.ns), YESNO(s.vad), YESNO(s.agc), YESNO(s.wakenet), s.linear_gain);
  ESP_LOGI(TAG, "AFE frames: channels=%d microphones=%d references=%d mic_ids=%d,%d ref_id=%d feed=%d process=%d fetch=%d counters_available=%s",
           s.channels, s.microphones, s.references, s.mic0, s.mic1, s.ref, s.feed, s.process, s.fetch,
           YESNO(ESP_AFE_DIAGNOSTIC_COUNTERS));
#ifdef USE_ESP_AUDIO_STACK_CAPTURE
  ESP_LOGI(TAG, "AFE last valid fetch (independent values): trigger_channel_id=%d raw_data_channels=%d data_size_bytes=%d; channels describe DSP outputs, not physical ADC",
           s.fetch_trigger_channel, s.fetch_raw_channels, s.fetch_data_size);
#endif
  if constexpr (ESP_AFE_DIAGNOSTIC_COUNTERS) {
    const auto &t = s.counters;
    ESP_LOGI(TAG, "AFE counters: frames=%u output_miss=%u feed_ok=%u feed_rejected=%u fetch_ok=%u fetch_timeout=%u input_drop=%u output_drop=%u feed_queued=%u fetch_queued=%u",
             (unsigned) t.frame_count, (unsigned) t.glitch_count, (unsigned) t.feed_ok, (unsigned) t.feed_rejected,
             (unsigned) t.fetch_ok, (unsigned) t.fetch_timeout, (unsigned) t.input_ring_drop,
             (unsigned) t.output_ring_drop, (unsigned) t.feed_queue_frames, (unsigned) t.fetch_queue_frames);
  }
#else
  ESP_LOGI(TAG, "AFE diagnostics omitted: enable audio_stack telemetry or PCM capture");
#endif
}

}  // namespace esphome::esp_afe

#endif  // !CLANG_TIDY
#endif  // USE_ESP32
