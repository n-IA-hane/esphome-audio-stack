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
#include "afe_input_frame.h"

namespace esphome::esp_afe {

static const char *const TAG = "esp_afe";

static inline void silence_frame(int16_t *out, int output_samples) {
  if (out != nullptr && output_samples > 0) {
    memset(out, 0, static_cast<size_t>(output_samples) * sizeof(int16_t));
  }
}

bool EspAfe::process(const int16_t *in_mic, const int16_t *in_ref, int16_t *out, uint8_t mic_channels_in) {
  return this->process_(in_mic, in_ref, out, mic_channels_in, nullptr);
}

bool EspAfe::process_planar(const int16_t *const *in_mics, const int16_t *in_ref, int16_t *out,
                           uint8_t mic_channels_in) {
  const bool valid = in_mics != nullptr && (mic_channels_in <= 1 || in_mics[1] != nullptr);
  return this->process_(valid ? in_mics[0] : nullptr, in_ref, out, mic_channels_in,
                        valid && mic_channels_in > 1 ? in_mics[1] : nullptr);
}

bool EspAfe::process_(const int16_t *in_mic, const int16_t *in_ref, int16_t *out, uint8_t mic_channels_in,
                      const int16_t *in_secondary) {
  const int transport_mic_channels = std::max<int>(1, mic_channels_in);
  const int primary_stride = in_secondary != nullptr ? 1 : transport_mic_channels;
  if (out == nullptr) {
    return false;
  }

  // Publish the busy guard before reading any field owned by the live AFE
  // instance. With the matching seq_cst drain protocol, teardown either waits
  // for this frame or this frame observes drain_request_ and touches no live
  // pointer or size.
  this->process_busy_.store(true, std::memory_order_seq_cst);
  if (this->drain_request_.load(std::memory_order_seq_cst) || !this->accepting_processing_()) {
    const int published_output = this->last_spec_fetch_size_.load(std::memory_order_acquire);
    this->clear_process_busy_();
    silence_frame(out, published_output);
    return false;
  }

  int qs = this->process_chunksize_ > 0 ? this->process_chunksize_ : this->fetch_chunksize_;
  int os = this->fetch_chunksize_;
  if (in_mic == nullptr) {
    this->clear_process_busy_();
    silence_frame(out, os);
    return false;
  }
  // Fast path when user has disabled every AFE feature: the instance is torn
  // down, but the caller still expects a frame-shaped output. Emit silence
  // rather than leaking raw pre-AFE mic audio to MWW, VA or call components.
  if (this->afe_stopped_.load(std::memory_order_acquire)) {
    const int published_output = this->last_spec_fetch_size_.load(std::memory_order_acquire);
    this->clear_process_busy_();
    silence_frame(out, published_output > 0 ? published_output : os);
    return false;
  }
  if (!this->is_initialized()) {
    this->clear_process_busy_();
    silence_frame(out, os);
    return false;
  }

  diag_add(this->process_frame_count_);

  const int64_t process_start_us = ESP_AFE_TIMING_TELEMETRY ? esp_timer_get_time() : 0;
  auto finish_process_timing = [this, process_start_us]() {
    if constexpr (ESP_AFE_TIMING_TELEMETRY) {
      uint32_t elapsed_us = static_cast<uint32_t>(std::max<int64_t>(0, esp_timer_get_time() - process_start_us));
      this->process_us_last_.store(elapsed_us, std::memory_order_relaxed);
      update_peak_atomic(this->process_us_max_, elapsed_us);
    }
  };

  int fs = this->feed_chunksize_;
  bool gmf_path = false;
#ifdef USE_ESP_AFE_GMF_PATH
  gmf_path = this->feed_input_ring_ != nullptr;
#endif
  if (qs <= 0 || os <= 0 || fs <= 0 || (!gmf_path && this->feed_buf_ == nullptr)) {
    silence_frame(out, os);
    this->clear_process_busy_();
    finish_process_timing();
    return false;
  }

  // Step 1: stage new input and feed it to AFE when a full frame is assembled.
  int offset = this->staged_input_samples_;
  if (offset < 0 || offset >= fs) {
    ESP_LOGW(TAG, "Invalid AFE staging offset %d for feed size %d", offset, fs);
    offset = 0;
  }
  // Drop any partial frame staged with a different channel count: mixing
  // two layouts in feed_buf_ would feed the AFE garbage if a caller changes
  // the transport channel layout without recreating the instance first.
  if (this->last_process_mic_channels_ != 0 && this->last_process_mic_channels_ != transport_mic_channels &&
      offset > 0) {
    ESP_LOGD(TAG, "process(): mic_channels_in changed %d -> %d, resetting staged input",
             this->last_process_mic_channels_, transport_mic_channels);
    offset = 0;
  }
  this->last_process_mic_channels_ = transport_mic_channels;

  const bool sample_rms = (this->input_volume_sensor_enabled_ || this->output_rms_sensor_enabled_) &&
                          !this->warmup_remaining_ && qs > 0 && (++this->rms_sensor_divider_ >= 8);
  if (sample_rms) {
    this->rms_sensor_divider_ = 0;
  }

  // Input RMS: compute on the transport mic before feeding the AFE pipeline.
  // Replaces ESP-SR's app-level data_volume signal. Pass stride so we
  // read mic1 samples only when the transport delivers interleaved
  // channels (otherwise the RMS mixes mic1 + mic2 + reference and the
  // dBFS sensor reports nonsense).
  if (sample_rms && this->input_volume_sensor_enabled_) {
    this->input_volume_dbfs_.store(esp_audio_stack::compute_rms_dbfs_i16(in_mic, static_cast<size_t>(qs),
                                                                         static_cast<size_t>(primary_stride)),
                                   std::memory_order_relaxed);
  }

  const int tc = this->total_channels_;
  const size_t feed_bytes = static_cast<size_t>(fs) * this->total_channels_ * sizeof(int16_t);
  int source_offset = 0;
  while (source_offset < qs) {
    const int stage_samples = std::min(qs - source_offset, fs - offset);
    const int16_t *mic_chunk = in_mic + source_offset * primary_stride;
    const int16_t *ref_chunk = in_ref == nullptr ? nullptr : in_ref + source_offset;
    const int16_t *secondary_chunk = in_secondary == nullptr ? nullptr : in_secondary + source_offset;
#ifdef USE_ESP_AFE_GMF_PATH
    void *gmf_slot = this->staged_feed_slot_;
#endif
    bool staged = true;
#ifdef USE_ESP_AFE_GMF_PATH
    if (gmf_path) {
      if (gmf_slot == nullptr) {
        gmf_slot = this->acquire_gmf_feed_slot_(feed_bytes, 0);
        this->staged_feed_slot_ = static_cast<int16_t *>(gmf_slot);
      }
      if (gmf_slot == nullptr) {
        diag_add(this->input_ring_drop_);
        staged = false;
      } else {
        stage_afe_input_frame(static_cast<int16_t *>(gmf_slot) + offset * tc, mic_chunk, ref_chunk, stage_samples,
                              transport_mic_channels, this->afe_config_->pcm_config, secondary_chunk);
        offset += stage_samples;
      }
    } else
#endif
        if (this->feed_buf_ != nullptr) {
      int16_t *dst = this->feed_buf_ + offset * tc;
      stage_afe_input_frame(dst, mic_chunk, ref_chunk, stage_samples, transport_mic_channels,
                            this->afe_config_->pcm_config, secondary_chunk);
      offset += stage_samples;
    } else {
      diag_add(this->input_ring_drop_);
      staged = false;
    }

    if (staged && offset == fs) {
      if (this->warmup_remaining_ > 0) {
        this->warmup_remaining_--;
      }
#ifdef USE_ESP_AFE_GMF_PATH
      {
        // Enqueue the full frame into the NOSPLIT ring. WS3-style GMF input uses
        // a direct ring slot, so the I2S task avoids staging and xRingbufferSend
        // copying. Fallback keeps partial-frame topologies on the old staging
        // path.
        if (gmf_slot != nullptr) {
          if (!this->commit_gmf_feed_slot_(gmf_slot)) {
            diag_add(this->input_ring_drop_);
          } else {
            this->staged_feed_slot_ = nullptr;
            uint32_t queued = diag_increment_and_get(this->feed_queue_frames_);
            update_peak_atomic(this->feed_queue_peak_, queued);
          }
        } else if (this->feed_input_ring_ != nullptr && this->feed_buf_ != nullptr) {
          if (!xRingbufferSend(this->feed_input_ring_, this->feed_buf_, feed_bytes, 0)) {
            diag_add(this->input_ring_drop_);
          } else {
            uint32_t queued = diag_increment_and_get(this->feed_queue_frames_);
            update_peak_atomic(this->feed_queue_peak_, queued);
          }
        }
      }
#else
      {
        diag_add(this->feed_rejected_);
      }
#endif
      offset = 0;
    }
    source_offset += stage_samples;
  }
  this->staged_input_samples_ = offset;

  // Step 2: try to pull a processed frame that the fetch task has pushed into
  // our side of the bridge. Non-blocking: if nothing is ready we emit silence,
  // never raw pre-AFE mic. MWW, VA and call components must only see processed
  // AFE output while this component is active.
  size_t output_bytes = static_cast<size_t>(os) * sizeof(int16_t);
  bool processed = false;
  if (this->fetch_output_ring_) {
    if (!this->output_prebuffer_ready_) {
      const size_t required_frames = static_cast<size_t>(this->output_prebuffer_frames_) + 1U;
      // Preserve the native DSP prebuffer duration when transport slices are
      // smaller. The same output FIFO serves both frame sizes.
      const size_t native_frame_bytes = std::max(output_bytes, static_cast<size_t>(this->feed_chunksize_) * sizeof(int16_t));
      const size_t queued_frames = gmf_path ? this->fetch_output_ring_->available() / native_frame_bytes
                                            : this->fetch_output_ring_->nosplit_items_waiting();
      this->output_prebuffer_ready_ = queued_frames >= required_frames;
    }
  }
  // Only this consumer removes bytes. Manager results and consumer frames can
  // have different sizes; leave bytes queued until a complete frame is ready.
  if (this->fetch_output_ring_ && this->output_prebuffer_ready_ &&
      (!gmf_path || this->fetch_output_ring_->available() >= output_bytes)) {
    size_t got = this->fetch_output_ring_->read(reinterpret_cast<uint8_t *>(out), output_bytes, 0);
    if (got == output_bytes) {
#ifdef USE_ESP_AFE_POST_AGC
      // Run post-AFE AGC only after a complete consumer frame is assembled,
      // independently of the manager's native output block size.
      if (this->post_afe_agc_ != nullptr &&
          !this->process_post_afe_agc_frame_(out, out, static_cast<size_t>(os))) {
        ESP_LOGW(TAG, "Post-AFE AGC failed; affected 10ms blocks bypassed");
      }
#endif
      processed = true;
      decrement_if_nonzero(this->fetch_queue_frames_);
      this->update_fetch_ring_free_pct_();
    }
  }
  if (!processed) {
    diag_add(this->process_output_miss_);
    silence_frame(out, os);
  }

  // Release drain guard BEFORE emitting telemetry / counters: those touch
  // only atomics on this and can safely race with a rebuild.
  this->clear_process_busy_();

  // Output-side RMS depends on the samples handed to the caller. Input volume
  // is computed before feeding GMF, and VAD state comes from the manager result.
  if (processed && sample_rms && this->output_rms_sensor_enabled_) {
    this->output_rms_dbfs_.store(esp_audio_stack::compute_rms_dbfs_i16(out, os), std::memory_order_relaxed);
  }
  finish_process_timing();
  return processed;
}

#ifdef USE_ESP_AFE_GMF_PATH
int32_t EspAfe::manager_read_cb_(void *buffer, int bytes, void *ctx, uint32_t ticks) {
  auto *self = static_cast<EspAfe *>(ctx);
  if (self == nullptr || buffer == nullptr || bytes <= 0) {
    return 0;
  }
  esp_gmf_payload_t payload{};
  payload.buf = static_cast<uint8_t *>(buffer);
  payload.buf_length = static_cast<uint32_t>(bytes);
  if (self->gmf_input_acquire_(&payload, static_cast<uint32_t>(bytes), static_cast<int>(ticks)) != ESP_GMF_IO_OK) {
    return 0;
  }
  return static_cast<int32_t>(payload.valid_size);
}

void EspAfe::manager_result_cb_(afe_fetch_result_t *result, void *ctx) {
  auto *self = static_cast<EspAfe *>(ctx);
  // Follow ESP-SR's documented fetch contract: ESP_FAIL denotes failure.
  if (self == nullptr || result == nullptr || result->ret_value == ESP_FAIL || result->data == nullptr ||
      result->data_size <= 0 || result->data_size % sizeof(int16_t) != 0) {
    return;
  }
  if (!self->accepting_processing_() ||
      self->drain_request_.load(std::memory_order_acquire)) {
    return;
  }
#ifdef USE_ESP_AUDIO_STACK_CAPTURE
  self->last_fetch_trigger_channel_id_.store(result->trigger_channel_id, std::memory_order_relaxed);
  self->last_fetch_raw_data_channels_.store(result->raw_data_channels, std::memory_order_relaxed);
  self->last_fetch_data_size_.store(result->data_size, std::memory_order_relaxed);
#endif
  self->voice_present_.store(self->vad_enabled_.load(std::memory_order_relaxed) && result->vad_state == VAD_SPEECH,
                             std::memory_order_relaxed);
  esp_gmf_payload_t payload{};
  payload.buf = reinterpret_cast<uint8_t *>(result->data);
  payload.valid_size = result->data_size;
  payload.buf_length = result->data_size;
  self->gmf_output_release_(&payload, 0);
#ifdef USE_ESP_AUDIO_STACK_AFE_OUTPUT_CAPTURE
  // Preserve selected PCM admission first. Vendor outputs remain borrowed
  // only until this callback returns; the observer copies into its own queue.
  // Stereo capture accepts at most 16384 PCM bytes, so this bound also keeps
  // all interleaved offset arithmetic within the proven three-channel span.
  if (self->raw_capture_parent_ != nullptr && result->raw_data != nullptr && result->raw_data_channels == 3 &&
      result->trigger_channel_id >= 0 && result->trigger_channel_id < 3 && result->data_size <= 8192) {
    self->raw_capture_parent_->capture_afe_outputs(result->raw_data, result->data,
                                                 result->data_size / sizeof(int16_t));
  }
#endif
}

esp_gmf_err_io_t EspAfe::gmf_input_acquire_(esp_gmf_payload_t *load, uint32_t wanted_size, int wait_ticks) {
  if (load == nullptr || load->buf == nullptr || wanted_size == 0 || this->feed_input_ring_ == nullptr) {
    return ESP_GMF_IO_FAIL;
  }
  // Rebuild drains input while microphone consumers remain registered. Both
  // lifecycle states must release the same blocking GMF reader.
  const auto accepting_input = [this]() {
    return this->accepting_processing_() &&
           !this->drain_request_.load(std::memory_order_acquire);
  };
  const bool active_at_entry = accepting_input();
  TickType_t read_wait = static_cast<TickType_t>(wait_ticks);
  if (!active_at_entry) {
    // The read callback maps interruption to no bytes, not valid PCM. Do not
    // synthesize zero frames while the caller is trying to pause: the manager's
    // higher-priority feed task could starve that caller.
    // The existing flush item wakes this read. Bound an otherwise infinite
    // wait by one 16-kHz AFE frame so callback detachment can finish.
    const uint32_t frame_ms = static_cast<uint32_t>(std::max(1, this->feed_chunksize_) + 15) / 16;
    const TickType_t frame_wait = std::max<TickType_t>(1, pdMS_TO_TICKS(frame_ms));
    read_wait = std::min(read_wait, frame_wait);
  }

  size_t item_size = 0;
  void *item = xRingbufferReceive(this->feed_input_ring_, &item_size, read_wait);
  if (item == nullptr) {
    if (!active_at_entry || !accepting_input()) {
      load->valid_size = 0;
      return ESP_GMF_IO_ABORT;
    }
    diag_add(this->feed_rejected_);
    return ESP_GMF_IO_TIMEOUT;
  }

  decrement_if_nonzero(this->feed_queue_frames_);
  if (!accepting_input()) {
    vRingbufferReturnItem(this->feed_input_ring_, item);
    load->valid_size = 0;
    return ESP_GMF_IO_ABORT;
  }
  esp_gmf_err_io_t ret = ESP_GMF_IO_FAIL;
  if (item_size == static_cast<size_t>(wanted_size) && load->buf_length >= wanted_size) {
    const int64_t feed_start_us = ESP_AFE_TIMING_TELEMETRY ? esp_timer_get_time() : 0;
    memcpy(load->buf, item, item_size);
    load->valid_size = item_size;

    if constexpr (ESP_AFE_TIMING_TELEMETRY) {
      uint32_t feed_us = static_cast<uint32_t>(std::max<int64_t>(0, esp_timer_get_time() - feed_start_us));
      this->feed_us_last_.store(feed_us, std::memory_order_relaxed);
      update_peak_atomic(this->feed_us_max_, feed_us);
    }
    diag_add(this->feed_ok_);
    ret = ESP_GMF_IO_OK;
  } else {
    ESP_LOGW(TAG, "GMF AFE input size mismatch (%u != %u, buf=%u)", static_cast<unsigned>(item_size),
             static_cast<unsigned>(wanted_size), static_cast<unsigned>(load->buf_length));
    diag_add(this->feed_rejected_);
  }

  vRingbufferReturnItem(this->feed_input_ring_, item);
  return ret;
}

esp_gmf_err_io_t EspAfe::gmf_output_release_(esp_gmf_payload_t *load, int wait_ticks) {
  (void) wait_ticks;
  if (!this->accepting_processing_()) {
    return ESP_GMF_IO_OK;
  }
  if (load == nullptr || load->buf == nullptr || load->valid_size == 0) {
    diag_add(this->fetch_timeout_);
    return ESP_GMF_IO_OK;
  }
  if (!this->fetch_output_ring_) {
    return ESP_GMF_IO_FAIL;
  }

  const size_t frame_bytes = static_cast<size_t>(this->fetch_chunksize_) * sizeof(int16_t);
  if (frame_bytes == 0) {
    return ESP_GMF_IO_FAIL;
  }

  // Native AFE output blocks need not match the consumer frame size. Preserve
  // every sample in the single bridge; process() consumes complete frames.

  const size_t pending_bytes = this->fetch_output_ring_->available() % frame_bytes;
  const size_t wrote = this->fetch_output_ring_->write_without_replacement(load->buf, load->valid_size, 0, false);
  if (wrote != load->valid_size) {
    diag_add(this->output_ring_drop_);
  } else {
    diag_add(this->fetch_ok_, (pending_bytes + wrote) / frame_bytes);
    const uint32_t queued = this->fetch_output_ring_->available() / frame_bytes;
    this->fetch_queue_frames_.store(queued, std::memory_order_relaxed);
    update_peak_atomic(this->fetch_queue_peak_, queued);
  }
  this->update_fetch_ring_free_pct_();
  return ESP_GMF_IO_OK;
}

#endif

void EspAfe::update_fetch_ring_free_pct_() {
  if constexpr (ESP_AFE_DIAGNOSTIC_COUNTERS) {
    if (!this->fetch_output_ring_) {
      this->ringbuf_free_pct_.store(1.0f, std::memory_order_relaxed);
      return;
    }
    const size_t free_bytes = this->fetch_output_ring_->free();
    const size_t used_bytes = this->fetch_output_ring_->available();
    const size_t total_bytes = free_bytes + used_bytes;
    if (total_bytes > 0) {
      this->ringbuf_free_pct_.store(static_cast<float>(free_bytes) / static_cast<float>(total_bytes),
                                    std::memory_order_relaxed);
    }
  }
}

#ifdef USE_ESP_AFE_GMF_PATH
void EspAfe::flush_pipeline_before_stop_() {
  if (this->feed_input_ring_ == nullptr || this->feed_chunksize_ <= 0 || this->total_channels_ <= 0) {
    return;
  }
  // Wake a blocked reader so callback detachment need not wait for its timeout.
  // Input is discarded once processing is inactive or the drain has begun.
  const size_t bytes = static_cast<size_t>(this->feed_chunksize_) * this->total_channels_ * sizeof(int16_t);
  void *slot = this->acquire_gmf_feed_slot_(bytes, 0);
  if (slot != nullptr) {
    memset(slot, 0, bytes);
    this->commit_gmf_feed_slot_(slot);
  }
}

void *EspAfe::acquire_gmf_feed_slot_(size_t feed_bytes, TickType_t ticks_to_wait) {
  if (this->feed_input_ring_ == nullptr || feed_bytes == 0) {
    return nullptr;
  }
  void *slot = nullptr;
  if (xRingbufferSendAcquire(this->feed_input_ring_, &slot, feed_bytes, ticks_to_wait) != pdTRUE) {
    return nullptr;
  }
  return slot;
}

bool EspAfe::commit_gmf_feed_slot_(void *slot) {
  if (this->feed_input_ring_ == nullptr || slot == nullptr) {
    return false;
  }
  return xRingbufferSendComplete(this->feed_input_ring_, slot) == pdTRUE;
}

bool EspAfe::prepare_feed_input_ring_() {
  if (this->afe_manager_ == nullptr || this->feed_chunksize_ <= 0) {
    return false;
  }

  if (this->feed_input_ring_ == nullptr) {
    // NOSPLIT ring: xRingbufferSend is atomic per-item; Receive returns a
    // complete frame. BRIDGE_RING_FRAMES capacity absorbs BSS jitter without
    // memory bloat. Each NOSPLIT item adds an 8-byte header, included in
    // sizing.
    const size_t frame_bytes = static_cast<size_t>(this->feed_chunksize_) * this->total_channels_ * sizeof(int16_t);
    const size_t ring_size = (frame_bytes + RINGBUFFER_ITEM_HEADER_BYTES) * BRIDGE_RING_FRAMES;
    const uint32_t feed_ring_caps =
        this->feed_ring_in_psram_ ? (MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT) : (MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    this->feed_input_ring_storage_ = static_cast<uint8_t *>(heap_caps_malloc(ring_size, feed_ring_caps));
    if (this->feed_input_ring_storage_ == nullptr && this->feed_ring_in_psram_) {
      ESP_LOGW(TAG,
               "feed_input_ring (%u bytes) fell back to internal RAM (PSRAM "
               "full/unavailable)",
               (unsigned) ring_size);
      this->feed_input_ring_storage_ =
          static_cast<uint8_t *>(heap_caps_malloc(ring_size, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    }
    if (this->feed_input_ring_storage_ == nullptr) {
      ESP_LOGE(TAG, "Failed to allocate AFE feed input ring storage (%u bytes)", (unsigned) ring_size);
      return false;
    }
    this->feed_input_ring_struct_ = static_cast<StaticRingbuffer_t *>(
        heap_caps_malloc(sizeof(StaticRingbuffer_t), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    if (this->feed_input_ring_struct_ == nullptr) {
      heap_caps_free(this->feed_input_ring_storage_);
      this->feed_input_ring_storage_ = nullptr;
      ESP_LOGE(TAG, "Failed to allocate AFE feed input ring struct");
      return false;
    }
    this->feed_input_ring_ = xRingbufferCreateStatic(ring_size, RINGBUF_TYPE_NOSPLIT, this->feed_input_ring_storage_,
                                                     this->feed_input_ring_struct_);
    if (this->feed_input_ring_ == nullptr) {
      heap_caps_free(this->feed_input_ring_storage_);
      this->feed_input_ring_storage_ = nullptr;
      heap_caps_free(this->feed_input_ring_struct_);
      this->feed_input_ring_struct_ = nullptr;
      ESP_LOGE(TAG, "Failed to create AFE feed input ring");
      return false;
    }
    ESP_LOGI(TAG, "Feed input ring: %u bytes (%u per frame, %u slots, NOSPLIT)", (unsigned) ring_size,
             (unsigned) frame_bytes, (unsigned) BRIDGE_RING_FRAMES);
  }
  return true;
}
#endif

#ifdef USE_ESP_AFE_GMF_PATH
void EspAfe::drain_feed_input_ring_() {
  if (this->feed_input_ring_ == nullptr) {
    this->feed_queue_frames_.store(0, std::memory_order_relaxed);
    return;
  }
  // Called after callback detachment or before callback installation. Complete
  // the owned reservation so Receive can discard it, never expose a partial
  // frame to the next acquisition, and never leave the NOSPLIT writer pinned.
  if (this->staged_feed_slot_ != nullptr) {
    this->commit_gmf_feed_slot_(this->staged_feed_slot_);
    this->staged_feed_slot_ = nullptr;
  }
  this->staged_input_samples_ = 0;
  uint32_t dropped = 0;
  while (true) {
    size_t item_size = 0;
    void *item = xRingbufferReceive(this->feed_input_ring_, &item_size, 0);
    if (item == nullptr) {
      break;
    }
    dropped++;
    vRingbufferReturnItem(this->feed_input_ring_, item);
  }
  this->feed_queue_frames_.store(0, std::memory_order_relaxed);
  if (dropped > 0) {
    diag_add(this->feed_rejected_, dropped);
    ESP_LOGD(TAG, "Dropped %u stale AFE feed frame(s) while stopping", (unsigned) dropped);
  }
}
#endif

bool EspAfe::prepare_fetch_output_ring_() {
  bool has_instance = false;
#ifdef USE_ESP_AFE_GMF_PATH
  has_instance = has_instance || this->afe_manager_ != nullptr;
#endif
  if (!has_instance || this->fetch_chunksize_ <= 0) {
    return false;
  }

  if (!this->fetch_output_ring_) {
    const size_t frame_bytes = static_cast<size_t>(std::max(this->feed_chunksize_, this->fetch_chunksize_)) * sizeof(int16_t);
    const size_t ring_bytes = (frame_bytes + RINGBUFFER_ITEM_HEADER_BYTES) * BRIDGE_RING_FRAMES;
#ifdef USE_ESP_AFE_GMF_PATH
    if (this->afe_manager_ != nullptr) {
      // GMF payload boundaries are independent of the consumer frame size.
      this->fetch_output_ring_ =
          this->fetch_ring_in_psram_
              ? esp_audio_stack::create_prefer_psram(ring_bytes, "esp_afe.fetch_output_ring")
              : esp_audio_stack::create_internal(ring_bytes, "esp_afe.fetch_output_ring");
    } else
#endif
    {
      // The direct AFE fetch API returns complete frames.
      this->fetch_output_ring_ =
          this->fetch_ring_in_psram_
              ? esp_audio_stack::create_nosplit_prefer_psram(ring_bytes, "esp_afe.fetch_output_ring")
              : esp_audio_stack::create_nosplit_internal(ring_bytes, "esp_afe.fetch_output_ring");
    }
    if (!this->fetch_output_ring_) {
      ESP_LOGE(TAG, "Failed to allocate AFE fetch output ring buffer");
      return false;
    }
    this->reset_output_prebuffer_();
  }

  return true;
}

}  // namespace esphome::esp_afe

#endif  // !CLANG_TIDY
#endif  // USE_ESP32
